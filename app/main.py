from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, select

from app.config import Settings
from app.creation import CampaignInput, CampaignResult, create_campaign
from app.keitaro import CatalogError, CatalogItem, KeitaroClient
from app.models import Campaign


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


@app.post("/api/campaigns", response_model=CampaignResult)
async def submit_campaign(request: Request, fields: CampaignInput, client: Client) -> CampaignResult:
    # This local operator interface can use an upstream credential to write.
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "Создание доступно только через локальный адрес приложения.")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Запрос должен прийти со страницы приложения.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Требуется application/json.")
    return await create_campaign(client, request.app.state.engine, request.app.state.settings, fields)
