"""Edit a stream's local draft without changing its published offer bindings."""

from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.keitaro import CatalogItem
from app.models import Campaign, Offer, Stream, StreamOffer, StreamOfferDraft


@dataclass
class Binding:
    offer_id: int
    share: int
    state: str
    removed: bool
    pinned: bool


def _bindings(db: Session, model: type[StreamOffer] | type[StreamOfferDraft], stream_id: int) -> list[Binding]:
    # A former occupant of a position precedes the live row which took its place.
    records = db.scalars(select(model).where(model.stream_id == stream_id).order_by(
        model.position, model.removed.desc(), model.offer_id,
    ))
    return [Binding(r.offer_id, r.share, r.state, r.removed, r.pinned) for r in records]


def _distribute(rows: list[Binding]) -> None:
    active = [r for r in rows if not r.removed and r.state == "active"]
    if not active:
        raise HTTPException(409, "В потоке должен остаться хотя бы один активный оффер.")
    pinned = [r for r in active if r.pinned]
    free = [r for r in active if not r.pinned]
    remaining = 100 - sum(r.share for r in pinned)
    if any(r.share <= 0 for r in pinned) or remaining < len(free) or (not free and remaining != 0):
        raise HTTPException(409, "Невозможно распределить 100%. Измените или снимите закрепление доли.")
    if free:
        each, extra = divmod(remaining, len(free))
        for index, row in enumerate(free):
            row.share = each + (index >= len(free) - extra)


def overlay_drafts(db: Session, snapshot: dict) -> dict:
    streams = {s["id"]: s for s in snapshot["streams"] if s["has_draft"]}
    if streams:
        for stream in streams.values():
            stream["offers"] = []
        query = (select(StreamOfferDraft, Offer.name)
                 .join(Offer, StreamOfferDraft.offer_id == Offer.id)
                 .where(StreamOfferDraft.stream_id.in_(streams))
                 .order_by(StreamOfferDraft.stream_id, StreamOfferDraft.removed,
                           StreamOfferDraft.position, StreamOfferDraft.offer_id))
        for row, name in db.execute(query):
            streams[row.stream_id]["offers"].append({
                "offer_id": row.offer_id, "name": name, "share": row.share, "state": row.state,
                "removed": row.removed, "pinned": row.pinned,
            })
    return snapshot


def clear_drafts(db: Session, stream_ids: Iterable[int]) -> None:
    ids = list(stream_ids)
    if ids:
        db.execute(delete(StreamOfferDraft).where(StreamOfferDraft.stream_id.in_(ids)))
        db.execute(update(Stream).where(Stream.id.in_(ids), Stream.draft_base.is_not(None)).values(
            draft_base=None, draft_add_origin=None, edit_revision=Stream.edit_revision + 1,
        ))


def _edit_rows(
    rows: list[Binding], published: list[Binding], stream: Stream, action: str, offer_id: int,
    share: int | None,
) -> None:
    row = next((r for r in rows if r.offer_id == offer_id), None)
    if action == "add":
        if row is not None:
            raise HTTPException(409, "Этот оффер уже есть в потоке или его архиве.")
        if stream.draft_add_origin is None:
            # Later removing only the newly added offers must also undo their redistribution.
            stream.draft_add_origin = [[r.offer_id, r.share] for r in rows]
        rows.append(Binding(offer_id, 0, "active", False, False))
    else:
        if row is None:
            raise HTTPException(409, "В выбранном потоке нет такого оффера.")
        if action == "remove" and not row.removed:
            if any(r.offer_id == offer_id for r in published):
                row.removed, row.pinned, row.share = True, False, 0
            else:
                rows.remove(row)
                origin = stream.draft_add_origin
                if origin is not None and [r.offer_id for r in rows] == [item[0] for item in origin]:
                    for restored, (_, original_share) in zip(rows, origin):
                        if not restored.removed and restored.state == "active":
                            restored.share = original_share
                    stream.draft_add_origin = None
                    return
            if row.state == "disabled":
                # Archiving a disabled binding leaves the active allocation and its undo data intact.
                return
        elif action == "restore" and row.removed:
            row.removed, row.pinned, row.state = False, False, "active"
        elif action in ("pin", "unpin") and row.state == "active" and not row.removed:
            if action == "pin":
                value = row.share if share is None else share
                if type(value) is not int or not 1 <= value <= 100:
                    raise HTTPException(422, "Закреплённая доля должна быть целым числом от 1 до 100.")
                row.share, row.pinned = value, True
            else:
                if not row.pinned:
                    raise HTTPException(409, "У этого оффера нет закреплённой доли.")
                row.pinned = False
        else:
            raise HTTPException(409, "Это действие недоступно для текущего состояния оффера.")
        # Preserve independent edits to offers that existed before this series of additions.
        if stream.draft_add_origin is not None and any(item[0] == offer_id for item in stream.draft_add_origin):
            stream.draft_add_origin = None
        if action == "unpin":
            return
    _distribute(rows)


def edit_draft(
    engine: Engine, campaign_id: int, stream_id: int, action: str, offer_id: int | None,
    expected_revision: int, share: int | None = None, catalog_item: CatalogItem | None = None,
) -> None:
    with Session(engine) as db, db.begin():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        found = db.execute(select(Stream, Campaign.creation_state).join(Campaign).where(
            Campaign.keitaro_id == campaign_id, Stream.id == stream_id, Stream.present.is_(True),
        )).one_or_none()
        if found is None:
            raise HTTPException(404, "Поток не найден в сохранённой кампании.")
        stream, creation_state = found
        if stream.edit_revision != expected_revision:
            raise HTTPException(409, "Поток уже изменился. Откройте актуальный снимок и повторите действие.")
        if creation_state not in ("ready", "imported") or stream.schema != "landings":
            raise HTTPException(409, "Редактирование доступно для потоков офферов готовой кампании.")
        if action == "cancel":
            clear_drafts(db, [stream_id])
            return
        if offer_id is None:
            raise HTTPException(422, "Укажите ID оффера.")

        published = _bindings(db, StreamOffer, stream_id)
        if stream.draft_base is None:
            rows = [Binding(r.offer_id, r.share, r.state, r.removed, r.pinned) for r in published]
        else:
            rows = _bindings(db, StreamOfferDraft, stream_id)
        _edit_rows(rows, published, stream, action, offer_id, share)
        if rows == published:
            clear_drafts(db, [stream_id])
            return
        if action == "add":
            if catalog_item is None or catalog_item.id != offer_id or not catalog_item.name.strip():
                raise HTTPException(422, "Оффер отсутствует в справочнике Keitaro.")
            db.merge(Offer(id=offer_id, name=catalog_item.name))
            db.flush()
        if stream.draft_base is None:
            stream.draft_base = [
                {"offer_id": r.offer_id, "share": r.share, "state": r.state}
                for r in published if not r.removed
            ]
        stream.edit_revision += 1
        db.execute(delete(StreamOfferDraft).where(StreamOfferDraft.stream_id == stream_id))
        if rows:
            db.execute(insert(StreamOfferDraft), [
                {"stream_id": stream_id, "offer_id": r.offer_id, "position": pos,
                 "share": r.share, "state": r.state, "removed": r.removed, "pinned": r.pinned}
                for pos, r in enumerate(rows)
            ])
