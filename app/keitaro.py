import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError


class CatalogItem(BaseModel):
    id: int
    name: str


_catalog = TypeAdapter(list[CatalogItem])


class CatalogError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class KeitaroClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _catalog(self, path: str, **query: str) -> list[CatalogItem]:
        try:
            response = await self.http.get(path, params=query)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise CatalogError("Keitaro did not respond in time.", 504) from exc
        except httpx.HTTPStatusError as exc:
            raise CatalogError(f"Keitaro returned HTTP {exc.response.status_code}.") from exc
        except httpx.RequestError as exc:
            raise CatalogError("Could not reach the Keitaro API.") from exc

        try:
            return _catalog.validate_json(response.content)
        except ValidationError as exc:
            raise CatalogError("Keitaro returned an invalid catalog.") from exc

    async def domains(self) -> list[CatalogItem]:
        return await self._catalog("domains")

    async def campaign_groups(self) -> list[CatalogItem]:
        return await self._catalog("groups", type="campaigns")

    async def traffic_sources(self) -> list[CatalogItem]:
        return await self._catalog("traffic_sources")

    async def offers(self, query: str = "") -> list[CatalogItem]:
        offers = await self._catalog("offers")
        term = query.strip().casefold()
        if not term:
            return offers
        return [item for item in offers if term == str(item.id) or term in item.name.casefold()]
