"""Publish a single local draft, using a verified tracker read as the commit point."""

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.drafts import clear_drafts
from app.importer import RemoteStream
from app.keitaro import CatalogError, KeitaroClient
from app.models import Campaign, Stream, StreamOffer, StreamOfferDraft


@dataclass(frozen=True)
class PushPlan:
    baseline: list[dict]
    target: list[dict]
    pending: bool
    lock_path: Path


def _prepare(engine: Engine, campaign_id: int, stream_id: int, revision: int) -> PushPlan:
    with Session(engine) as db:
        connection = db.connection()
        connection.exec_driver_sql("BEGIN")
        result = db.execute(select(Stream, Campaign.creation_state).join(Campaign).where(
            Campaign.keitaro_id == campaign_id, Stream.id == stream_id, Stream.present.is_(True),
        )).one_or_none()
        if result is None:
            raise HTTPException(404, "Поток не найден в сохранённой кампании.")
        stream, campaign_state = result
        if stream.edit_revision != revision:
            raise HTTPException(409, "Версия потока изменилась. Откройте актуальное состояние.")
        if stream.schema != "landings" or campaign_state not in ("ready", "imported"):
            raise HTTPException(409, "Для этого потока публикация недоступна.")
        if stream.draft_base is None:
            raise HTTPException(409, "У потока нет неопубликованного черновика.")
        rows = db.scalars(select(StreamOfferDraft).where(
            StreamOfferDraft.stream_id == stream_id, StreamOfferDraft.removed.is_(False),
        ).order_by(StreamOfferDraft.position, StreamOfferDraft.offer_id))
        target = [{"offer_id": row.offer_id, "share": row.share, "state": row.state} for row in rows]
        filename = connection.exec_driver_sql("PRAGMA database_list").first()[2]
        if not filename:  # An in-memory database is private to this process.
            filename = Path(tempfile.gettempdir()) / f"keitaro-{os.getpid()}-{id(engine)}"
        return PushPlan(stream.draft_base, target, stream.push_pending, Path(f"{filename}.push-{stream_id}.sqlite3"))


@contextmanager
def _push_lock(path: Path):
    # An OS-released SQLite lock survives multiple app workers without holding the app DB.
    lock = sqlite3.connect(path, timeout=0)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise HTTPException(409, "Публикация этого потока уже выполняется.") from exc
            raise
        yield
    finally:
        lock.close()


def _advance(
    engine: Engine, stream_id: int, revision: int, action: Literal["reserve", "release", "confirm"],
) -> int:
    with Session(engine) as db, db.begin():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        stream = db.get(Stream, stream_id)
        if stream is None or stream.edit_revision != revision or stream.draft_base is None:
            raise HTTPException(409, "Состояние черновика изменилось. Откройте сохранённые потоки.")
        if action == "confirm":
            # The frozen working rows include local pins and the archive; neither travels in the PUT.
            columns = ("stream_id", "offer_id", "position", "share", "state", "removed", "pinned")
            rows = select(*(getattr(StreamOfferDraft, key) for key in columns)).where(
                StreamOfferDraft.stream_id == stream_id,
            )
            db.execute(delete(StreamOffer).where(StreamOffer.stream_id == stream_id))
            db.execute(insert(StreamOffer).from_select(columns, rows))
            stream.push_pending = False
            clear_drafts(db, [stream_id])  # Also advances edit_revision.
        else:
            stream.push_pending = action == "reserve"
            stream.edit_revision += 1
        # Reject in-flight Fetch calls which started before this transition.
        db.execute(update(Campaign).where(Campaign.id == stream.campaign_id).values(
            snapshot_revision=Campaign.snapshot_revision + 1,
        ))
        return revision + 1


async def _tracker_offers(client: KeitaroClient, campaign_id: int, stream_id: int) -> list[dict]:
    try:
        raw = await client.get_stream(stream_id)
        remote = RemoteStream.model_validate(raw)
    except CatalogError as exc:
        if exc.upstream_status == 404:
            raise HTTPException(409, "Поток больше не найден в Keitaro. Сверьте кампанию через Fetch.") from exc
        raise
    except ValidationError as exc:
        raise CatalogError("Keitaro returned incomplete stream data.") from exc
    if remote.id != stream_id or remote.campaign_id != campaign_id or remote.flow_schema != "landings":
        raise HTTPException(409, "Поток или его схема в Keitaro не соответствуют выбранному потоку.")
    return [item.model_dump(include={"offer_id", "share", "state"}) for item in remote.offers]


async def _verify(
    client: KeitaroClient, engine: Engine, campaign_id: int, stream_id: int, revision: int, plan: PushPlan,
) -> None:
    try:
        current = await _tracker_offers(client, campaign_id, stream_id)
    except HTTPException:
        await run_in_threadpool(_advance, engine, stream_id, revision, "release")
        raise
    # A transport or malformed-response error leaves the intent pending for a read-only retry.
    if current == plan.target:
        await run_in_threadpool(_advance, engine, stream_id, revision, "confirm")
        return
    await run_in_threadpool(_advance, engine, stream_id, revision, "release")
    if current == plan.baseline:
        raise HTTPException(409, "В Keitaro остался исходный состав. Для новой попытки нажмите Push ещё раз.")
    raise HTTPException(409, "В Keitaro другой состав. Черновик сохранён. Сверьте поток перед публикацией.")


async def publish_stream(
    client: KeitaroClient, engine: Engine, campaign_id: int, stream_id: int, expected_revision: int,
) -> None:
    plan = await run_in_threadpool(_prepare, engine, campaign_id, stream_id, expected_revision)
    with _push_lock(plan.lock_path):
        if plan.pending:
            await _verify(client, engine, campaign_id, stream_id, expected_revision, plan)
            return
        current = await _tracker_offers(client, campaign_id, stream_id)
        if current != plan.baseline:
            raise HTTPException(409, "Опубликованный состав изменился в Keitaro. Черновик сохранён.")
        if current == plan.target:
            await run_in_threadpool(_advance, engine, stream_id, expected_revision, "confirm")
            return
        # Persist intent only after preflight. From here edits and discard must wait for verification.
        revision = await run_in_threadpool(_advance, engine, stream_id, expected_revision, "reserve")
        try:
            await client.put_stream_offers(stream_id, plan.target)
        except CatalogError as exc:
            if exc.request_not_sent or exc.upstream_status in (400, 401, 403, 404, 406, 422):
                await run_in_threadpool(_advance, engine, stream_id, revision, "release")
                raise
            # An ambiguous PUT may already have applied. Read back before deciding the outcome.
        await _verify(client, engine, campaign_id, stream_id, revision, plan)
