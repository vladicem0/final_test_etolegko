from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select

from app.config import Settings
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
            yield
    finally:
        engine.dispose()


app = FastAPI(title="Keitaro Campaign Wrapper", lifespan=lifespan)


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
