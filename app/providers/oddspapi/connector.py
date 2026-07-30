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
    """Read-only OddsPapi v4 connector for fixture discovery and current odds."""

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
                    "from": start_time.isoformat(),
                    "to": end_time.isoformat(),
                    "statusId": 0,
                    "hasOdds": "true",
                    "bookmakers": ",".join(self._bookmakers),
                },
            ),
        )
        records = [
            ProviderEvent(
                provider=Provider.ODDSPAPI,
                provider_event_id=str(item["fixtureId"]),
                title=f"{item['participant1Name']} vs {item['participant2Name']}",
                category=str(item["tournamentName"]),
                scheduled_start=datetime.fromisoformat(
                    str(item["startTime"]).replace("Z", "+00:00")
                ),
                status=str(item["statusName"]),
            )
            for item in payload
            if int(item["statusId"]) == 0 and str(item["sportName"]).casefold() == "soccer"
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
                "/odds",
                {
                    "fixtureId": event_id,
                    "bookmakers": ",".join(self._bookmakers),
                    "oddsFormat": "decimal",
                },
            ),
        )
        records: list[ProviderSportsbookQuote] = []
        bookmaker_odds = cast(dict[str, dict[str, Any]], payload.get("bookmakerOdds", {}))
        for bookmaker, bookmaker_payload in bookmaker_odds.items():
            markets = cast(dict[str, dict[str, Any]], bookmaker_payload.get("markets", {}))
            for market_id, market in markets.items():
                outcomes = cast(dict[str, dict[str, Any]], market.get("outcomes", {}))
                for outcome_id, outcome in outcomes.items():
                    players = cast(dict[str, dict[str, Any]], outcome.get("players", {}))
                    for raw in players.values():
                        records.append(
                            ProviderSportsbookQuote(
                                provider_event_id=event_id,
                                bookmaker_id=bookmaker,
                                provider_outcome_id=int(outcome_id),
                                bookmaker_outcome_id=(
                                    str(raw["bookmakerOutcomeId"])
                                    if raw.get("bookmakerOutcomeId") is not None
                                    else None
                                ),
                                market_id=int(market_id),
                                decimal_odds=Decimal(str(raw["price"])),
                                active=bool(raw["active"]),
                                market_active=bool(market["marketActive"]),
                                main_line=bool(raw.get("mainLine", False)),
                                changed_at=datetime.fromisoformat(
                                    str(raw["changedAt"]).replace("Z", "+00:00")
                                ),
                            )
                        )
        return records

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
