from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, select

from app.config import Settings
from app.creation import CampaignInput, CampaignResult, create_campaign
from app.drafts import edit_draft
from app.importer import fetch_campaign, local_campaign
from app.keitaro import CatalogError, CatalogItem, KeitaroClient
from app.models import Campaign
from app.publishing import publish_stream


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(select(Campaign).limit(0))
        async with httpx.AsyncClient(
            base_url=str(settings.keitaro_base_url).rstrip("/") + "/",
            headers={"Api-Key": settings.keitaro_api_key.get_secret_value()},
            timeout=10.0,
        ) as http:
            application.state.keitaro = KeitaroClient(http)
            application.state.engine = engine
            application.state.settings = settings
            yield
    finally:
        engine.dispose()


app = FastAPI(title="Keitaro Campaign Wrapper", lifespan=lifespan)
app_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=app_dir / "templates")
app.mount("/static", StaticFiles(directory=app_dir / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def creator_form(request: Request):
    return templates.TemplateResponse(request=request, name="create.html")


@app.get("/campaigns", response_class=HTMLResponse)
async def campaign_browser(request: Request):
    return templates.TemplateResponse(request=request, name="import.html")


def get_keitaro(request: Request) -> KeitaroClient:
    return request.app.state.keitaro


Client = Annotated[KeitaroClient, Depends(get_keitaro)]


@app.exception_handler(CatalogError)
async def catalog_error(request: Request, exc: CatalogError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


@app.get("/api/domains")
async def domains(client: Client) -> list[CatalogItem]:
    return await client.domains()


@app.get("/api/groups")
async def groups(client: Client) -> list[CatalogItem]:
    return await client.campaign_groups()


@app.get("/api/traffic-sources")
async def traffic_sources(client: Client) -> list[CatalogItem]:
    return await client.traffic_sources()


@app.get("/api/offers")
async def offers(client: Client, q: str = "") -> list[CatalogItem]:
    return await client.offers(q)


def require_local_write(request: Request) -> None:
    # Writes belong to the local operator, including updates to the SQLite snapshot.
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "Операция доступна только через локальный адрес приложения.")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Запрос должен прийти со страницы приложения.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Требуется application/json.")


@app.post("/api/campaigns", response_model=CampaignResult, dependencies=[Depends(require_local_write)])
async def submit_campaign(request: Request, fields: CampaignInput, client: Client) -> CampaignResult:
    return await create_campaign(client, request.app.state.engine, request.app.state.settings, fields)


CampaignId = Annotated[int, PathParam(gt=0, le=2**63 - 1)]


@app.get("/api/campaigns")
async def campaigns(
    client: Client, offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[CatalogItem]:
    return await client.list_campaigns(offset, limit)


@app.get("/api/campaigns/{campaign_id}/streams")
def saved_streams(request: Request, campaign_id: CampaignId) -> dict:
    snapshot = local_campaign(request.app.state.engine, campaign_id)
    return {**snapshot, "admin_url": request.app.state.settings.campaign_admin_url(campaign_id)}


Revision = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
PositiveId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class FetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discard_drafts: dict[PositiveId, Revision] | None = None


@app.post("/api/campaigns/{campaign_id}/fetch", dependencies=[Depends(require_local_write)])
async def fetch_streams(
    request: Request, campaign_id: CampaignId, client: Client, fields: FetchInput | None = None,
) -> dict:
    snapshot = await fetch_campaign(
        client, request.app.state.engine, campaign_id, fields.discard_drafts if fields else None,
    )
    return {**snapshot, "admin_url": request.app.state.settings.campaign_admin_url(campaign_id)}


class DraftAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "remove", "restore", "pin", "unpin", "cancel"]
    expected_revision: Revision
    offer_id: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)] | None = None
    share: Annotated[int, Field(strict=True, ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def check_action_fields(self):
        if (self.action == "cancel") != (self.offer_id is None):
            raise ValueError("Supply an offer ID for offer actions only.")
        if self.share is not None and self.action != "pin":
            raise ValueError("Only pin accepts an explicit share.")
        return self


@app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/draft", dependencies=[Depends(require_local_write)])
async def update_draft(
    request: Request, campaign_id: CampaignId, stream_id: CampaignId, fields: DraftAction, client: Client,
) -> dict:
    catalog_item = await client.offer(fields.offer_id) if fields.action == "add" else None
    engine = request.app.state.engine
    await run_in_threadpool(
        edit_draft, engine, campaign_id, stream_id, action=fields.action, offer_id=fields.offer_id,
        expected_revision=fields.expected_revision, share=fields.share, catalog_item=catalog_item,
    )
    snapshot = await run_in_threadpool(local_campaign, engine, campaign_id)
    return {**snapshot, "admin_url": request.app.state.settings.campaign_admin_url(campaign_id)}


class PushInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Revision


@app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/push", dependencies=[Depends(require_local_write)])
async def push_stream(
    request: Request, campaign_id: CampaignId, stream_id: CampaignId, fields: PushInput, client: Client,
) -> dict:
    engine = request.app.state.engine
    await publish_stream(client, engine, campaign_id, stream_id, fields.expected_revision)
    snapshot = await run_in_threadpool(local_campaign, engine, campaign_id)
    return {**snapshot, "admin_url": request.app.state.settings.campaign_admin_url(campaign_id)}
