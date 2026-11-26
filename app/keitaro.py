from typing import Annotated, Any, TypeVar

import httpx
from pydantic import AfterValidator, BaseModel, TypeAdapter, ValidationError


class CatalogItem(BaseModel):
    id: int
    name: str


def _require_id(record: dict[str, Any]) -> dict[str, Any]:
    remote_id = record.get("id")
    if type(remote_id) is not int or remote_id <= 0:
        raise ValueError("Expected a positive numeric ID.")
    return record


# Keep the full remote record: recovery also compares fields beyond the ID.
Record = Annotated[dict[str, Any], AfterValidator(_require_id)]
T = TypeVar("T")

_catalog = TypeAdapter(list[CatalogItem])
_offer = TypeAdapter(CatalogItem)
_record = TypeAdapter(Record)
_records = TypeAdapter(list[Record])


class CatalogError(Exception):
    def __init__(
        self, message: str, status_code: int = 502, *, upstream_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = upstream_status


class KeitaroClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _request(
        self, method: str, path: str, adapter: TypeAdapter[T], *,
        params: dict[str, str | int] | None = None, payload: dict | None = None,
    ) -> T:
        try:
            response = await self.http.request(method, path, params=params, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise CatalogError("Keitaro did not respond in time.", 504) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise CatalogError(f"Keitaro returned HTTP {status}.", upstream_status=status) from exc
        except httpx.RequestError as exc:
            raise CatalogError("Could not reach the Keitaro API.") from exc

        try:
            return adapter.validate_json(response.content)
        except ValidationError as exc:
            description = "an invalid catalog" if adapter is _catalog else "an invalid response"
            raise CatalogError(f"Keitaro returned {description}.") from exc

    async def domains(self) -> list[CatalogItem]:
        return await self._request("GET", "domains", _catalog)

    async def campaign_groups(self) -> list[CatalogItem]:
        return await self._request("GET", "groups", _catalog, params={"type": "campaigns"})

    async def traffic_sources(self) -> list[CatalogItem]:
        return await self._request("GET", "traffic_sources", _catalog)

    async def offers(self, query: str = "") -> list[CatalogItem]:
        offers = await self._request("GET", "offers", _catalog)
        term = query.strip().casefold()
        if not term:
            return offers
        return [item for item in offers if term == str(item.id) or term in item.name.casefold()]

    async def offer(self, offer_id: int) -> CatalogItem:
        try:
            offer = await self._request("GET", f"offers/{offer_id}", _offer)
        except CatalogError as exc:
            if exc.upstream_status == 404:
                raise CatalogError("The selected offer does not exist.", 404, upstream_status=404) from exc
            raise
        if offer.id != offer_id:
            raise CatalogError("Keitaro returned a different offer.")
        return offer

    async def campaigns_by_alias(self, alias: str) -> list[dict]:
        matches = []
        seen_ids = set()
        offset = 0
        while True:
            page = await self._request(
                "GET", "campaigns", _records, params={"offset": offset, "limit": 100}
            )
            if not page:
                return matches
            for campaign in page:
                if not isinstance(campaign.get("alias"), str) or not campaign["alias"]:
                    raise CatalogError("Keitaro returned a campaign without an alias.")
                if campaign["id"] in seen_ids:
                    raise CatalogError("Keitaro returned overlapping campaign pages.")
                seen_ids.add(campaign["id"])
                if campaign["alias"] == alias:
                    matches.append(campaign)
            offset += len(page)

    async def campaign(self, campaign_id: int) -> dict:
        campaign = await self._request("GET", f"campaigns/{campaign_id}", _record)
        if campaign["id"] != campaign_id:
            raise CatalogError("Keitaro returned a different campaign.")
        return campaign

    async def campaign_streams(self, campaign_id: int) -> list[dict]:
        streams = await self._request("GET", f"campaigns/{campaign_id}/streams", _records)
        if any(stream.get("campaign_id") != campaign_id for stream in streams):
            raise CatalogError("Keitaro returned a stream with a mismatched campaign ID.")
        return streams

    async def create_campaign(self, payload: dict) -> dict:
        return await self._request("POST", "campaigns", _record, payload=payload)

    async def create_stream(self, payload: dict) -> dict:
        return await self._request("POST", "streams", _record, payload=payload)
