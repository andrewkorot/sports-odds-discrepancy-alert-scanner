from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
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

SPORT_ID_SOCCER = 10
logger = logging.getLogger("uvicorn.error")


class OddsPapiHTTPError(RuntimeError):
    """HTTP failure that deliberately excludes the credential-bearing URL."""

    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"OddsPapi HTTP {status_code}")


def sanitized_error(exc: Exception) -> str:
    if isinstance(exc, OddsPapiHTTPError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"OddsPapi HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"OddsPapi request failed: {type(exc).__name__}"
    return f"OddsPapi response error: {type(exc).__name__}"


class SportsOddsConnector:
    """Read-only OddsPapi v4 connector for fixture discovery and current odds."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        bookmaker_slugs: list[str],
        client: httpx.AsyncClient | None = None,
        mode: str = "live",
        discovery_dump_path: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._bookmakers = bookmaker_slugs
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None
        self._health = ProviderHealthRecord(
            provider=Provider.ODDSPAPI, mode=mode, enabled=True, connected=False
        )
        self._market_catalog: dict[tuple[int, int], tuple[str, str, str]] = {}
        self._market_catalog_loaded = False
        self._market_catalog_lock = asyncio.Lock()
        self._bookmaker_catalog: tuple[list[Bookmaker], list[str]] | None = None
        self._canonical_by_provider_id: dict[str, str] = {}
        self._last_odds_request_started = 0.0
        self._discovery_dump_path = Path(discovery_dump_path) if discovery_dump_path else None

    @staticmethod
    def _write_discovery_dump(path: Path, document: dict[str, Any]) -> None:
        """Atomically replace the latest raw fixture snapshot."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)

    async def _dump_discovered_events(
        self,
        payload: list[dict[str, Any]],
        start_time: datetime,
        end_time: datetime,
        provider_slugs: list[str],
    ) -> None:
        if self._discovery_dump_path is None:
            return
        document = {
            "provider": Provider.ODDSPAPI.value,
            "fetched_at_utc": datetime.now(UTC).isoformat(),
            "window_start_utc": start_time.isoformat(),
            "window_end_utc": end_time.isoformat(),
            "bookmaker_slugs": provider_slugs,
            "record_count": len(payload),
            "records": payload,
        }
        try:
            await asyncio.to_thread(
                self._write_discovery_dump,
                self._discovery_dump_path,
                document,
            )
        except OSError as exc:
            # A diagnostics export must not make the live scan fail.
            logger.warning(
                "oddspapi.discovery_dump.failed path=%s error_type=%s",
                self._discovery_dump_path,
                type(exc).__name__,
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
            safe_exception: Exception = exc
            if isinstance(exc, httpx.HTTPStatusError):
                safe_exception = OddsPapiHTTPError(
                    exc.response.status_code,
                    exc.response.headers.get("retry-after"),
                )
            self._health = self._health.model_copy(
                update={
                    "connected": False,
                    "last_attempt_at": attempted,
                    "last_failure_at": datetime.now(UTC),
                    "consecutive_failures": self._health.consecutive_failures + 1,
                    "latest_error_code": type(safe_exception).__name__,
                    "sanitized_latest_error": sanitized_error(safe_exception),
                }
            )
            if safe_exception is not exc:
                raise safe_exception from None
            raise

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        fixture_params: dict[str, Any] = {
            "sportId": SPORT_ID_SOCCER,
            "from": start_time.isoformat(),
            "to": end_time.isoformat(),
            "statusId": 0,
            "hasOdds": "true",
        }
        # OddsPapi expects its own bookmaker slugs here, not our canonical IDs.
        # The catalog is mapped before discovery in the live pipeline, so this
        # limits discovery to fixtures having odds at any configured bookmaker.
        provider_slugs = sorted(self._canonical_by_provider_id)
        if provider_slugs:
            fixture_params["bookmakers"] = ",".join(provider_slugs)

        payload = cast(
            list[dict[str, Any]],
            await self._get("/fixtures", fixture_params),
        )
        await self._dump_discovered_events(
            payload,
            start_time,
            end_time,
            provider_slugs,
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
                sport=str(item["sportName"]).casefold(),
                competition=str(item["tournamentName"]),
                home_team=str(item["participant1Name"]),
                away_team=str(item["participant2Name"]),
            )
            for item in payload
            if int(item["statusId"]) == 0 and str(item["sportName"]).casefold() == "soccer"
        ]
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        print(f"Discovered events from {self._base}: {len(records)}")
        # print(records)
        return records

    async def list_bookmakers(self) -> tuple[list[Bookmaker], list[str]]:
        if self._bookmaker_catalog is not None:
            return self._bookmaker_catalog
        payload = cast(list[dict[str, Any]], await self._get("/bookmakers"))
        self._bookmaker_catalog = map_provider_bookmakers(
            [
                ProviderBookmaker(
                    provider_id=str(item["slug"]),
                    name=str(item["bookmakerName"]),
                    active=bool(item.get("active", True)),
                )
                for item in payload
            ]
        )
        return self._bookmaker_catalog

    def use_provider_bookmaker_ids(
        self, mapped: list[Bookmaker], enabled_canonical_ids: list[str]
    ) -> None:
        enabled = set(enabled_canonical_ids)
        self._canonical_by_provider_id = {
            item.provider_bookmaker_id: item.canonical_id
            for item in mapped
            if item.canonical_id in enabled and item.provider_bookmaker_id
        }

    async def get_event_odds(self, event_id: str) -> list[ProviderSportsbookQuote]:
        await self._ensure_market_catalog()
        elapsed = perf_counter() - self._last_odds_request_started
        if elapsed < 0.55:
            await asyncio.sleep(0.55 - elapsed)
        self._last_odds_request_started = perf_counter()
        payload = cast(
            dict[str, Any],
            await self._get(
                "/odds",
                {
                    "fixtureId": event_id,
                    "oddsFormat": "decimal",
                },
            ),
        )
        records: list[ProviderSportsbookQuote] = []
        bookmaker_odds = cast(dict[str, dict[str, Any]], payload.get("bookmakerOdds", {}))
        for provider_bookmaker_id, bookmaker_payload in bookmaker_odds.items():
            bookmaker = self._canonical_by_provider_id.get(provider_bookmaker_id) or (
                provider_bookmaker_id if provider_bookmaker_id in self._bookmakers else None
            )
            if bookmaker is None:
                continue
            direct_url = (
                str(bookmaker_payload["fixturePath"])
                if bookmaker_payload.get("fixturePath")
                else None
            )
            markets = cast(dict[str, dict[str, Any]], bookmaker_payload.get("markets", {}))
            for market_id, market in markets.items():
                outcomes = cast(dict[str, dict[str, Any]], market.get("outcomes", {}))
                for outcome_id, outcome in outcomes.items():
                    semantics = self._market_catalog.get((int(market_id), int(outcome_id)))
                    if semantics is None:
                        continue
                    market_type, selection, period = semantics
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
                                market_type=market_type,
                                selection=selection,
                                period=period,
                                direct_url=direct_url,
                            )
                        )
        return records

    async def _ensure_market_catalog(self) -> None:
        if self._market_catalog_loaded:
            return
        async with self._market_catalog_lock:
            if self._market_catalog_loaded:
                return
            payload = cast(list[dict[str, Any]], await self._get("/markets", {"language": "en"}))
            selection_names = {"1": "home", "x": "draw", "2": "away"}
            for market in payload:
                if (
                    int(market.get("sportId", -1)) != SPORT_ID_SOCCER
                    or bool(market.get("playerProp"))
                    or str(market.get("period", "")).casefold() != "fulltime"
                    or str(market.get("marketType", "")).casefold() != "1x2"
                ):
                    continue
                for outcome in cast(list[dict[str, Any]], market.get("outcomes", [])):
                    selection = selection_names.get(str(outcome.get("outcomeName", "")).casefold())
                    if selection:
                        self._market_catalog[
                            (int(market["marketId"]), int(outcome["outcomeId"]))
                        ] = (
                            "moneyline",
                            selection,
                            "regulation",
                        )
            self._market_catalog_loaded = True

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
