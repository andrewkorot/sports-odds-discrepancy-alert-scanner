from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.providers.oddspapi.mapping import ProviderBookmaker


class OddsPapiBookmakerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slug: str
    bookmakerName: str
    active: bool = True


class OddsPapiClient:
    """Confirmed v5 REST boundary; credential is only sent as a query parameter."""

    def __init__(
        self, api_key: str, base_url: str = "https://v5.oddspapi.io/en", timeout: float = 15
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def _get(self, path: str, **params: Any) -> Any:
        response = await self._client.get(path, params={"apiKey": self._api_key, **params})
        response.raise_for_status()
        return response.json()

    async def list_bookmakers(self) -> list[ProviderBookmaker]:
        payload = [
            OddsPapiBookmakerPayload.model_validate(x) for x in await self._get("/bookmakers")
        ]
        return [
            ProviderBookmaker(provider_id=x.slug, name=x.bookmakerName, active=x.active)
            for x in payload
        ]

    async def list_sports(self) -> list[dict[str, Any]]:
        return list(await self._get("/sports"))

    async def list_fixtures(self, **filters: Any) -> list[dict[str, Any]]:
        return list(await self._get("/fixtures", **filters))

    async def get_fixture_odds(self, fixture_id: str) -> Any:
        # Confirmed by the v5 OpenAPI. Mapping remains at the provider boundary.
        return await self._get(f"/fixtures/{fixture_id}/odds")

    async def aclose(self) -> None:
        await self._client.aclose()
