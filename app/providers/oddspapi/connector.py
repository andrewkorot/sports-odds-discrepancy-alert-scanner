from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, cast

import httpx

from app.domain.enums import Provider
from app.domain.models import Bookmaker
from app.providers.oddspapi.mapping import (
    ProviderBookmaker,
    map_provider_bookmakers,
)
from app.providers.records import (
    ProviderEvent,
    ProviderHealthRecord,
    ProviderSportsbookQuote,
)


class SportsOddsConnector:
    """Read-only OddsPapi v5 connector for fixture discovery and current odds."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        bookmaker_slugs: list[str],
        client: httpx.AsyncClient | None = None,
        mode: str = "live",
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._bookmakers = bookmaker_slugs
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None
        self._health = ProviderHealthRecord(
            provider=Provider.ODDSPAPI, mode=mode, enabled=True, connected=False
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        attempted = datetime.now(UTC)
        started = perf_counter()
        try:
            response = await self._client.get(
                f"{self._base}{path}",
                params={"apiKey": self._api_key, **(params or {})},
            )
            response.raise_for_status()
            self._health = self._health.model_copy(
                update={
                    "connected": True,
                    "last_attempt_at": attempted,
                    "last_success_at": datetime.now(UTC),
                    "consecutive_failures": 0,
                    "latency_ms": Decimal(str((perf_counter() - started) * 1000)),
                }
            )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._health = self._health.model_copy(
                update={
                    "connected": False,
                    "last_attempt_at": attempted,
                    "last_failure_at": datetime.now(UTC),
                    "consecutive_failures": self._health.consecutive_failures + 1,
                    "latest_error_code": type(exc).__name__,
                    "sanitized_latest_error": str(exc)[:200],
                }
            )
            raise

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        payload = cast(
            list[dict[str, Any]],
            await self._get(
                "/fixtures",
                {
                    "sportId": 10,
                    "startTimeFrom": int(start_time.timestamp()),
                    "startTimeTo": int(end_time.timestamp()),
                    "bookmakers": ",".join(self._bookmakers),
                },
            ),
        )
        records = [
            ProviderEvent(
                provider=Provider.ODDSPAPI,
                provider_event_id=str(item["fixtureId"]),
                title=(
                    f"{item['participants']['participant1Name']} vs "
                    f"{item['participants']['participant2Name']}"
                ),
                category=str(item["tournament"]["tournamentName"]),
                scheduled_start=datetime.fromtimestamp(int(item["startTime"]), UTC),
                status=str(item["status"]["statusName"]),
            )
            for item in payload
            if not bool(item["status"]["live"])
            and str(item["sport"]["sportName"]).casefold() == "soccer"
        ]
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        return records

    async def list_bookmakers(self) -> tuple[list[Bookmaker], list[str]]:
        payload = cast(list[dict[str, Any]], await self._get("/bookmakers"))
        return map_provider_bookmakers(
            [
                ProviderBookmaker(
                    provider_id=str(item["slug"]),
                    name=str(item["bookmakerName"]),
                    active=bool(item.get("active", True)),
                )
                for item in payload
            ]
        )

    async def get_event_odds(self, event_id: str) -> list[ProviderSportsbookQuote]:
        payload = cast(
            dict[str, Any],
            await self._get(
                "/fixtures/odds",
                {
                    "fixtureId": event_id,
                    "bookmakers": ",".join(self._bookmakers),
                    "marketActive": "true",
                },
            ),
        )
        records: list[ProviderSportsbookQuote] = []
        for bookmaker, odds_by_id in cast(dict[str, Any], payload.get("odds", {})).items():
            for raw in cast(dict[str, dict[str, Any]], odds_by_id).values():
                changed_ms = int(raw["changedAt"])
                records.append(
                    ProviderSportsbookQuote(
                        provider_event_id=event_id,
                        bookmaker_id=bookmaker,
                        provider_outcome_id=int(raw["outcomeId"]),
                        bookmaker_outcome_id=(
                            str(raw["bookmakerOutcomeId"])
                            if raw.get("bookmakerOutcomeId") is not None
                            else None
                        ),
                        market_id=int(raw["marketId"]),
                        decimal_odds=Decimal(str(raw["price"])),
                        active=bool(raw["active"]),
                        market_active=bool(raw["marketActive"]),
                        main_line=bool(raw.get("mainLine", False)),
                        changed_at=datetime.fromtimestamp(changed_ms / 1000, UTC),
                    )
                )
        return records

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
