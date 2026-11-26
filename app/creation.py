from collections.abc import Awaitable, Callable
from uuid import UUID

import pycountry
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.keitaro import CatalogError, KeitaroClient
from app.models import Campaign


PREFIX = "test_nitsevich_"
GOOGLE_URL = "https://google.com"


class CampaignInput(BaseModel):
    request_id: UUID
    name: str
    geo_code: str
    offer_id: int = Field(gt=0, strict=True)

    @field_validator("name")
    @classmethod
    def campaign_name(cls, value: str) -> str:
        suffix = value.strip().removeprefix(PREFIX).strip()
        if not suffix or len(PREFIX + suffix) > 255:
            raise ValueError("Укажите название длиной до 255 символов вместе с префиксом.")
        return PREFIX + suffix

    @field_validator("geo_code")
    @classmethod
    def country(cls, value: str) -> str:
        raw = value.strip()
        code = raw.upper()
        if len(raw) != 2 or not raw.isascii() or pycountry.countries.get(alpha_2=code) is None:
            raise ValueError("Укажите двухбуквенный код страны, например AU.")
        return code


class StreamResult(BaseModel):
    id: int
    name: str
    position: int


class CampaignResult(BaseModel):
    campaign_id: int
    name: str
    alias: str
    geo_code: str
    offer_id: int
    streams: list[StreamResult]
    admin_url: str


def conflict(reason: str) -> HTTPException:
    return HTTPException(409, reason)


def operation(engine: Engine, fields: CampaignInput, settings: Settings) -> Campaign:
    alias = "kt-" + fields.request_id.hex
    with Session(engine, expire_on_commit=False) as db, db.begin():
        db.execute(insert(Campaign).values(
            name=fields.name, alias=alias, geo_code=fields.geo_code, offer_id=fields.offer_id,
            domain_id=settings.keitaro_domain_id, group_id=settings.keitaro_group_id,
            traffic_source_id=settings.keitaro_traffic_source_id, creation_state="new",
        ).on_conflict_do_nothing(index_elements=["alias"]))
        row = db.scalars(select(Campaign).where(Campaign.alias == alias)).one()
        if (row.name, row.geo_code, row.offer_id) != (fields.name, fields.geo_code, fields.offer_id):
            raise conflict("Этот идентификатор операции уже связан с другими параметрами.")
        return row


def checkpoint(engine: Engine, row: Campaign, state: str, **values) -> None:
    # Commit the intent before issuing a remote write, including across processes.
    values["creation_state"] = state
    with engine.begin() as connection:
        changed = connection.execute(
            update(Campaign).where(
                Campaign.id == row.id, Campaign.creation_state == row.creation_state
            ).values(**values)
        )
        if changed.rowcount != 1:
            raise conflict("Эта операция уже выполняется. Повторите её с тем же идентификатором.")
    for key, value in values.items():
        setattr(row, key, value)


async def validate_references(client: KeitaroClient, row: Campaign) -> None:
    for load, required_id, label in (
        (client.domains, row.domain_id, "домен"),
        (client.campaign_groups, row.group_id, "группу"),
        (client.traffic_sources, row.traffic_source_id, "источник"),
    ):
        if not any(item.id == required_id for item in await load()):
            raise CatalogError(f"Стенд не вернул настроенный справочник: {label} #{required_id}.")
    try:
        await client.offer(row.offer_id)
    except CatalogError as exc:
        if exc.upstream_status != 404:
            raise
        if row.creation_state == "new":
            raise HTTPException(422, "Выбранный оффер отсутствует на стенде.") from exc
        raise conflict("Выбранный оффер больше не доступен. Проверьте кампанию в Keitaro.") from exc


def campaign_fields(row: Campaign) -> dict:
    return {
        "name": row.name, "alias": row.alias, "type": "position",
        "domain_id": row.domain_id, "group_id": str(row.group_id),
        "traffic_source_id": row.traffic_source_id,
    }


def verify_campaign(remote: dict, row: Campaign) -> None:
    expected = campaign_fields(row)
    for key, value in expected.items():
        if str(remote.get(key)) != str(value):
            raise conflict(f"Настройка {key} кампании на стенде отличается от запроса. Проверьте кампанию.")


def expected_streams(row: Campaign) -> list[dict]:
    return [
        {
            "campaign_id": row.keitaro_id, "position": 1, "name": f"Geo {row.geo_code}",
            "type": "regular", "schema": "redirect", "state": "active",
            "action_type": "http", "action_payload": GOOGLE_URL,
            "filters": [{"name": "country", "mode": "accept", "payload": [row.geo_code]}],
        },
        {
            "campaign_id": row.keitaro_id, "position": 2, "name": "Offers",
            "type": "default", "schema": "landings", "state": "active",
            "offers": [{"offer_id": row.offer_id, "share": 100, "state": "active"}],
        },
    ]


def verify_streams(streams: list[dict], expected: list[dict]) -> None:
    if len(streams) > len(expected):
        raise conflict("В кампании обнаружен дополнительный поток.")
    for remote, target in zip(streams, expected):
        for key, value in target.items():
            if key in ("filters", "offers"):
                continue
            if remote.get(key) != value:
                raise conflict(f"У потока №{target['position']} отличается поле {key}.")
        for key in ("filters", "offers"):
            actual = remote.get(key)
            wanted = target.get(key, [])
            if not isinstance(actual, list) or len(actual) != len(wanted):
                raise conflict(f"У потока №{target['position']} отличается состав {key}.")
            for item, spec in zip(actual, wanted):
                if not isinstance(item, dict) or any(item.get(k) != v for k, v in spec.items()):
                    raise conflict(f"У потока №{target['position']} отличаются настройки {key}.")


async def send_step(
    engine: Engine, row: Campaign, in_flight: str,
    post: Callable[[dict], Awaitable[dict]], payload: dict,
) -> dict:
    previous = row.creation_state
    checkpoint(engine, row, in_flight)
    try:
        return await post(payload)
    except CatalogError as exc:
        # A definite rejection can be corrected and retried. A lost response cannot.
        if exc.upstream_status in (400, 401, 403, 404, 406, 422):
            checkpoint(engine, row, previous)
        raise


async def resolve_campaign(client: KeitaroClient, engine: Engine, row: Campaign) -> None:
    if row.keitaro_id is None:
        matches = await client.campaigns_by_alias(row.alias)
        if len(matches) > 1:
            raise conflict("Найдено несколько кампаний с alias этой операции.")
        if matches:
            remote = await client.campaign(matches[0]["id"])
            verify_campaign(remote, row)
        elif row.creation_state == "new":
            remote = await send_step(
                engine, row, "sending_campaign", client.create_campaign, campaign_fields(row)
            )
        else:
            raise conflict("Создание кампании ещё не подтверждено. Проверьте стенд и повторите с тем же идентификатором.")
        checkpoint(engine, row, "have_campaign", keitaro_id=remote["id"])
    verify_campaign(await client.campaign(row.keitaro_id), row)


async def create_campaign(
    client: KeitaroClient, engine: Engine, settings: Settings, fields: CampaignInput
) -> CampaignResult:
    row = operation(engine, fields, settings)
    try:
        return await complete_operation(client, engine, settings, row)
    except (CatalogError, HTTPException) as exc:
        if row.creation_state == "new":
            raise
        reference = f"Кампания #{row.keitaro_id}" if row.keitaro_id else "Операция создания"
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(exc.status_code, f"{reference} (alias {row.alias}). {detail}") from exc


async def complete_operation(
    client: KeitaroClient, engine: Engine, settings: Settings, row: Campaign
) -> CampaignResult:
    await validate_references(client, row)
    await resolve_campaign(client, engine, row)

    expected = expected_streams(row)
    streams = await client.campaign_streams(row.keitaro_id)
    verify_streams(streams, expected)
    steps = [
        ("have_campaign", "sending_geo", "have_geo"),
        ("have_geo", "sending_offer", "ready"),
    ]
    for index, (before, in_flight, after) in enumerate(steps):
        if len(streams) == index:
            if row.creation_state != before:
                raise conflict(f"Состояние потока №{index + 1} не подтверждено. Проверьте стенд и повторите с тем же идентификатором.")
            await send_step(engine, row, in_flight, client.create_stream, expected[index])
            streams = await client.campaign_streams(row.keitaro_id)
            verify_streams(streams, expected)
            if len(streams) <= index:
                raise conflict("Keitaro принял запрос, но созданный поток пока не виден.")
        if row.creation_state in (before, in_flight):
            checkpoint(engine, row, after)

    verify_campaign(await client.campaign(row.keitaro_id), row)
    host = str(settings.keitaro_base_url).split("/admin_api/", 1)[0].rstrip("/")
    return CampaignResult(
        campaign_id=row.keitaro_id, alias=row.alias, name=row.name, geo_code=row.geo_code,
        offer_id=row.offer_id, streams=[StreamResult.model_validate(s) for s in streams],
        admin_url=f"{host}/admin/#!/campaign.editor/{row.keitaro_id}",
    )
