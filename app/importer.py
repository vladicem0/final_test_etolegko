"""Import the confirmed state of a campaign using read-only Keitaro requests."""

from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import and_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.keitaro import CatalogError, KeitaroClient
from app.models import Campaign, Offer, Stream, StreamOffer


RemoteId = Annotated[int, Field(gt=0, le=2**63 - 1)]
NonemptyText = Annotated[str, Field(min_length=1)]


class RemoteRecord(BaseModel):
    model_config = ConfigDict(strict=True)


class RemoteCampaign(RemoteRecord):
    id: RemoteId
    name: NonemptyText
    alias: NonemptyText


class RemoteBinding(RemoteRecord):
    offer_id: RemoteId
    share: Annotated[int, Field(ge=0, le=100)]
    state: NonemptyText
    stream_id: RemoteId | None = None


class RemoteStream(RemoteRecord):
    id: RemoteId
    campaign_id: RemoteId
    name: NonemptyText
    position: Annotated[int, Field(ge=0)]
    type: NonemptyText
    flow_schema: NonemptyText = Field(alias="schema")
    offers: list[RemoteBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bindings(self):
        if "offers" not in self.model_fields_set and self.flow_schema not in ("redirect", "action"):
            raise ValueError("Missing published offers.")
        if len({item.offer_id for item in self.offers}) != len(self.offers):
            raise ValueError("Duplicate offer in stream.")
        if any(item.stream_id not in (None, self.id) for item in self.offers):
            raise ValueError("Offer belongs to a different stream.")
        return self


def _snapshot(db: Session, campaign_id: int) -> dict:
    # One SELECT also gives the reader a consistent view on SQLite.
    query = (
        select(Campaign, Stream, StreamOffer, Offer.name)
        .outerjoin(Stream, and_(Stream.campaign_id == Campaign.id, Stream.present.is_(True)))
        .outerjoin(StreamOffer, StreamOffer.stream_id == Stream.id)
        .outerjoin(Offer, Offer.id == StreamOffer.offer_id)
        .where(Campaign.keitaro_id == campaign_id, Campaign.snapshot_revision > 0)
        .order_by(Stream.position, Stream.id, StreamOffer.removed, StreamOffer.position, StreamOffer.offer_id)
    )
    result = None
    streams = {}
    for campaign, stream, binding, offer_name in db.execute(query):
        if result is None:
            result = {"id": campaign_id, "name": campaign.snapshot_name, "alias": campaign.snapshot_alias}
        if stream is None:
            continue
        if stream.id not in streams:
            streams[stream.id] = {
                "id": stream.id, "name": stream.name, "position": stream.position,
                "type": stream.type, "schema": stream.schema, "offers": [],
            }
        if binding is not None:
            streams[stream.id]["offers"].append({
                "offer_id": binding.offer_id, "name": offer_name, "share": binding.share,
                "state": binding.state, "removed": binding.removed,
            })
    if result is None:
        raise HTTPException(404, "Локального снимка кампании ещё нет. Нажмите Fetch.")
    return {**result, "streams": list(streams.values())}


def local_campaign(engine: Engine, campaign_id: int) -> dict:
    with Session(engine) as db:
        return _snapshot(db, campaign_id)


async def _read_remote(client: KeitaroClient, campaign_id: int):
    try:
        remote = await client.campaign(campaign_id)
    except CatalogError as exc:
        if exc.upstream_status == 404:
            raise HTTPException(404, "Кампания не найдена в Keitaro.") from exc
        raise
    rows = await client.campaign_streams(campaign_id)
    try:
        campaign = RemoteCampaign.model_validate(remote)
        streams = [RemoteStream.model_validate(row) for row in rows]
    except ValidationError as exc:
        raise CatalogError("Keitaro returned incomplete campaign or stream data.") from exc
    if campaign.id != campaign_id or any(stream.campaign_id != campaign_id for stream in streams):
        raise CatalogError("Keitaro returned data for a different campaign.")
    if len({stream.id for stream in streams}) != len(streams):
        raise CatalogError("Keitaro returned duplicate streams.")
    offer_ids = {binding.offer_id for stream in streams for binding in stream.offers}
    names = {}
    if offer_ids:
        for item in await client.offers():
            if item.id in offer_ids:
                if item.id in names or not item.name.strip():
                    raise CatalogError("Keitaro returned invalid offer catalog entries.")
                names[item.id] = item.name
    return campaign, streams, offer_ids, names


def _update_offers(db: Session, offer_ids: set[int], names: dict[int, str]) -> None:
    known = {offer.id: offer for offer in db.scalars(select(Offer).where(Offer.id.in_(offer_ids)))}
    for offer_id in offer_ids:
        if offer_id in names:
            if offer_id in known:
                known[offer_id].name = names[offer_id]
            else:
                db.add(Offer(id=offer_id, name=names[offer_id]))
        elif offer_id not in known:
            raise CatalogError(f"Keitaro did not return the name of offer #{offer_id}.")
    db.flush()


def _update_bindings(db: Session, stream: RemoteStream, previous: dict[int, StreamOffer]) -> None:
    for position, published in enumerate(stream.offers):
        binding = previous.pop(published.offer_id, None)
        if binding is None:
            binding = StreamOffer(stream_id=stream.id, offer_id=published.offer_id)
            db.add(binding)
        binding.position = position
        binding.share = published.share
        binding.state = published.state
        binding.removed = False
    for missing in previous.values():
        missing.share = 0
        missing.removed = True


async def fetch_campaign(client: KeitaroClient, engine: Engine, campaign_id: int) -> dict:
    with Session(engine) as db:
        revision = db.scalar(select(Campaign.snapshot_revision).where(Campaign.keitaro_id == campaign_id)) or 0
    remote, streams, offer_ids, names = await _read_remote(client, campaign_id)
    with Session(engine) as db, db.begin():
        # Take SQLite's write lock only after the upstream reads have completed.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = db.scalar(select(Campaign).where(Campaign.keitaro_id == remote.id))
        if (row.snapshot_revision if row else 0) != revision:
            raise HTTPException(409, "Снимок уже обновлён другим запросом. Повторите Fetch.")
        pending_creator = db.scalar(select(Campaign.id).where(
            Campaign.alias == remote.alias, Campaign.keitaro_id.is_(None),
        ))
        if pending_creator is not None:
            raise HTTPException(409, "Alias связан с незавершённым созданием. Проверьте состояние этой операции.")
        if row is None:
            row = Campaign(
                keitaro_id=remote.id, name=remote.name,
                creation_state="imported", snapshot_revision=0,
            )
            db.add(row)
            db.flush()
        row.snapshot_name = remote.name
        row.snapshot_alias = remote.alias
        row.snapshot_revision += 1
        _update_offers(db, offer_ids, names)

        known_streams = {
            s.id: s for s in db.scalars(select(Stream).where(Stream.campaign_id == row.id))
        }
        foreign_stream = db.scalar(select(Stream.id).where(
            Stream.id.in_([stream.id for stream in streams]), Stream.campaign_id != row.id,
        ).limit(1))
        if foreign_stream is not None:
            raise CatalogError(f"Keitaro returned stream #{foreign_stream} from another campaign.")
        bindings: dict[int, dict[int, StreamOffer]] = {}
        for binding in db.scalars(select(StreamOffer).where(StreamOffer.stream_id.in_(known_streams))):
            bindings.setdefault(binding.stream_id, {})[binding.offer_id] = binding
        for stream in streams:
            local = known_streams.pop(stream.id, None)
            if local is None:
                local = Stream(id=stream.id, campaign_id=row.id)
                db.add(local)
            local.name = stream.name
            local.position = stream.position
            local.type = stream.type
            local.schema = stream.flow_schema
            local.present = True
        for missing in known_streams.values():
            missing.present = False
        db.flush()
        for stream in streams:
            _update_bindings(db, stream, bindings.get(stream.id, {}))
        return _snapshot(db, campaign_id)
