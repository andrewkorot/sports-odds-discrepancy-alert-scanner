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

from app.domain.enums import AvailabilityStatus, Provider
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
MAX_BULK_TOURNAMENT_IDS = 5
BULK_ENDPOINT_COOLDOWN_SECONDS = 1.05
logger = logging.getLogger("uvicorn.error")
_MONEYLINE_OUTCOME_ALIASES = {
    "home": {"1", "home"},
    "draw": {"x", "draw", "tie"},
    "away": {"2", "away"},
}


def bookmaker_outcome_agrees(selection: str, bookmaker_outcome_id: str | None) -> bool:
    """Reject a recognizable bookmaker side that conflicts with catalog semantics.

    Opaque bookmaker IDs remain valid because the OddsPapi market/outcome catalog
    is authoritative. Only explicit home/draw/away labels can prove a conflict.
    """

    if bookmaker_outcome_id is None:
        return True
    normalized = bookmaker_outcome_id.strip().casefold()
    recognized = set().union(*_MONEYLINE_OUTCOME_ALIASES.values())
    return normalized not in recognized or normalized in _MONEYLINE_OUTCOME_ALIASES[selection]


class OddsPapiHTTPError(RuntimeError):
    """HTTP failure that deliberately excludes the credential-bearing URL."""

    def __init__(
        self,
        status_code: int,
        retry_after: str | None = None,
        provider_detail: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.provider_detail = provider_detail
        self.retry_after_seconds = retry_after_seconds
        message = f"OddsPapi HTTP {status_code}"
        if provider_detail:
            message = f"{message}: {provider_detail}"
        super().__init__(message)


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
        self._last_bulk_odds_request_completed = 0.0
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

    @staticmethod
    def _write_fixture_dump_preserving_bulk(path: Path, document: dict[str, Any]) -> None:
        """Replace fixture data without hiding the last completed bulk response."""
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and "odds_by_tournaments" in existing:
                document["odds_by_tournaments"] = existing["odds_by_tournaments"]
        SportsOddsConnector._write_discovery_dump(path, document)

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
            "bookmaker_filter": None,
            "configured_bookmaker_slugs": provider_slugs,
            "record_count": len(payload),
            "records": payload,
        }
        try:
            await asyncio.to_thread(
                self._write_fixture_dump_preserving_bulk,
                self._discovery_dump_path,
                document,
            )
        except (OSError, ValueError, TypeError) as exc:
            # A diagnostics export must not make the live scan fail.
            logger.warning(
                "oddspapi.discovery_dump.failed path=%s error_type=%s",
                self._discovery_dump_path,
                type(exc).__name__,
            )

    @staticmethod
    def _append_bulk_dump(path: Path, bulk_document: dict[str, Any]) -> None:
        """Add the latest raw bulk-odds responses to the fixture dump."""
        document: dict[str, Any] = {}
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                document = cast(dict[str, Any], existing)
        document["odds_by_tournaments"] = bulk_document
        SportsOddsConnector._write_discovery_dump(path, document)

    async def _dump_bulk_odds(
        self,
        tournament_ids: list[str],
        responses: list[dict[str, Any]],
    ) -> None:
        if self._discovery_dump_path is None:
            return
        bulk_document = {
            "fetched_at_utc": datetime.now(UTC).isoformat(),
            "tournament_ids": tournament_ids,
            "response_count": len(responses),
            "responses": responses,
        }
        try:
            await asyncio.to_thread(
                self._append_bulk_dump,
                self._discovery_dump_path,
                bulk_document,
            )
        except (OSError, ValueError, TypeError) as exc:
            # Diagnostics output must never prevent a pricing scan.
            logger.warning(
                "oddspapi.bulk_dump.failed path=%s error_type=%s",
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
                provider_detail = " ".join(exc.response.text.split())[:500]
                if self._api_key:
                    provider_detail = provider_detail.replace(self._api_key, "[redacted]")
                retry_after_seconds: float | None = None
                try:
                    error_payload = exc.response.json()
                    retry_ms = error_payload.get("error", {}).get("retryMs")
                    if retry_ms is not None:
                        retry_after_seconds = float(retry_ms) / 1000
                except (AttributeError, TypeError, ValueError):
                    pass
                safe_exception = OddsPapiHTTPError(
                    exc.response.status_code,
                    exc.response.headers.get("retry-after"),
                    provider_detail or None,
                    retry_after_seconds,
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

    async def _get_bulk_odds(self, params: dict[str, Any]) -> Any:
        """Pace bulk calls from response completion and retry one 429 safely."""
        for attempt in range(2):
            elapsed = perf_counter() - self._last_bulk_odds_request_completed
            if elapsed < BULK_ENDPOINT_COOLDOWN_SECONDS:
                await asyncio.sleep(BULK_ENDPOINT_COOLDOWN_SECONDS - elapsed)
            try:
                payload = await self._get("/odds-by-tournaments", params)
            except OddsPapiHTTPError as exc:
                self._last_bulk_odds_request_completed = perf_counter()
                if exc.status_code != 429 or attempt > 0:
                    raise
                retry_delay = max(
                    BULK_ENDPOINT_COOLDOWN_SECONDS,
                    (exc.retry_after_seconds or 0) + 0.1,
                )
                logger.warning(
                    "oddspapi.bulk.rate_limited retry_in_seconds=%.3f attempt=%d/2",
                    retry_delay,
                    attempt + 1,
                )
                await asyncio.sleep(retry_delay)
                # The explicit retry delay has already satisfied the cooldown.
                self._last_bulk_odds_request_completed = 0.0
                continue
            self._last_bulk_odds_request_completed = perf_counter()
            return payload
        raise RuntimeError("unreachable bulk retry state")

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        fixture_params: dict[str, Any] = {
            "sportId": SPORT_ID_SOCCER,
            "from": start_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "to": end_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "statusId": 0,
            "hasOdds": "true",
        }
        # Do not send a bookmaker filter here. OddsPapi applies a multi-bookmaker
        # fixture filter as an intersection (available at every supplied book),
        # which hides fixtures offered by only one configured bookmaker. The
        # hasOdds flag keeps discovery relevant; configured books are applied
        # later to bulk pricing after prediction-market matching.
        provider_slugs = sorted(self._canonical_by_provider_id)

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
                provider_competition_id=(
                    str(item["tournamentId"]) if item.get("tournamentId") is not None else None
                ),
                home_team=str(item["participant1Name"]),
                away_team=str(item["participant2Name"]),
            )
            for item in payload
            if int(item["statusId"]) == 0
            and str(item["sportName"]).casefold() == "soccer"
            and start_time
            <= datetime.fromisoformat(str(item["startTime"]).replace("Z", "+00:00"))
            < end_time
        ]
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        logger.info(
            "oddspapi.fixtures.complete raw_records=%d eligible_events=%d "
            "window_start=%s window_end=%s bookmaker_filter=none "
            "configured_bookmakers=%s",
            len(payload),
            len(records),
            start_time.isoformat(),
            end_time.isoformat(),
            ",".join(provider_slugs) or "all",
        )
        for record in records:
            logger.info(
                "oddspapi.fixture.event fixture_id=%s tournament_id=%s "
                "competition=%r home=%r away=%r kickoff=%s status=%s",
                record.provider_event_id,
                record.provider_competition_id,
                record.competition,
                record.home_team,
                record.away_team,
                record.scheduled_start.isoformat() if record.scheduled_start else None,
                record.status,
            )
        return records

    async def list_bookmakers(self) -> tuple[list[Bookmaker], list[str]]:
        if self._bookmaker_catalog is not None:
            return self._bookmaker_catalog
        payload = cast(list[dict[str, Any]], await self._get("/bookmakers"))
        provider_bookmakers = [
            ProviderBookmaker(
                provider_id=str(item["slug"]),
                name=str(item["bookmakerName"]),
                active=bool(item.get("active", True)),
            )
            for item in payload
        ]
        mapped, unknown = map_provider_bookmakers(provider_bookmakers)

        # The original canonical aliases remain stable, while additional
        # configured OddsPapi slugs are accepted only after the live bookmaker
        # catalog confirms them. This prevents invented provider identifiers.
        configured_slugs = {item.casefold(): item for item in self._bookmakers}
        mapped_provider_ids = {
            item.provider_bookmaker_id.casefold()
            for item in mapped
            if item.provider_bookmaker_id is not None
        }
        dynamically_mapped_ids: set[str] = set()
        for item in provider_bookmakers:
            provider_id = item.provider_id.casefold()
            configured_id = configured_slugs.get(provider_id)
            if configured_id is None or provider_id in mapped_provider_ids:
                continue
            mapped.append(
                Bookmaker(
                    canonical_id=configured_id,
                    display_name=item.name,
                    provider_bookmaker_id=item.provider_id,
                    availability_status=(
                        AvailabilityStatus.AVAILABLE
                        if item.active
                        else AvailabilityStatus.UNAVAILABLE
                    ),
                )
            )
            dynamically_mapped_ids.add(provider_id)

        unknown = [
            value
            for value in unknown
            if not any(f"({provider_id})" in value for provider_id in dynamically_mapped_ids)
        ]
        self._bookmaker_catalog = mapped, unknown
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
        return self._parse_event_odds(event_id, payload)

    async def get_events_odds(
        self, events: list[ProviderEvent]
    ) -> dict[str, list[ProviderSportsbookQuote]]:
        """Fetch odds for all relevant tournaments in one OddsPapi request.

        OddsPapi's bulk endpoint is tournament-based, so discovery retains each
        fixture's provider tournament ID. Returned fixtures are filtered back to
        the requested fixture IDs; unrelated fixtures from the same tournaments
        never enter comparison logic.
        """

        requested_event_ids = {event.provider_event_id for event in events}
        result: dict[str, list[ProviderSportsbookQuote]] = {
            event_id: [] for event_id in requested_event_ids
        }
        if not requested_event_ids:
            return result

        tournament_ids = sorted(
            {
                event.provider_competition_id
                for event in events
                if event.provider_competition_id is not None
            }
        )
        if not tournament_ids:
            raise ValueError("OddsPapi bulk odds require discovered tournament IDs")

        await self._ensure_market_catalog()
        common_params: dict[str, Any] = {
            "language": "en",
            "verbosity": 3,
            "oddsFormat": "decimal",
        }
        provider_slugs = sorted(self._canonical_by_provider_id)
        if not provider_slugs:
            raise ValueError("OddsPapi bulk odds require a mapped bookmaker")

        tournament_chunks = [
            tournament_ids[index : index + MAX_BULK_TOURNAMENT_IDS]
            for index in range(0, len(tournament_ids), MAX_BULK_TOURNAMENT_IDS)
        ]
        logger.info(
            "oddspapi.bulk.request events=%d tournament_ids=%s bookmakers=%s chunks=%d requests=%d",
            len(requested_event_ids),
            ",".join(tournament_ids),
            ",".join(provider_slugs),
            len(tournament_chunks),
            len(provider_slugs) * len(tournament_chunks),
        )
        for event in events:
            logger.info(
                "oddspapi.bulk.requested_event fixture_id=%s tournament_id=%s title=%r kickoff=%s",
                event.provider_event_id,
                event.provider_competition_id,
                event.title,
                event.scheduled_start.isoformat() if event.scheduled_start else None,
            )

        dumped_responses: list[dict[str, Any]] = []
        for provider_slug in provider_slugs:
            logger.info(
                "oddspapi.bulk.bookmaker.start bookmaker=%s tournaments=%d chunks=%d",
                provider_slug,
                len(tournament_ids),
                len(tournament_chunks),
            )
            bookmaker_quote_count = 0
            bookmaker_fixture_count = 0
            for chunk_index, tournament_chunk in enumerate(tournament_chunks, start=1):
                logger.info(
                    "oddspapi.bulk.chunk.start bookmaker=%s chunk=%d/%d tournament_ids=%s",
                    provider_slug,
                    chunk_index,
                    len(tournament_chunks),
                    ",".join(tournament_chunk),
                )
                try:
                    payload = await self._get_bulk_odds(
                        {
                            **common_params,
                            "tournamentIds": ",".join(tournament_chunk),
                            "bookmaker": provider_slug,
                        },
                    )
                    if isinstance(payload, list):
                        fixture_payloads = cast(list[dict[str, Any]], payload)
                    elif isinstance(payload, dict) and payload.get("fixtureId") is not None:
                        fixture_payloads = [cast(dict[str, Any], payload)]
                    else:
                        raise TypeError("Unexpected OddsPapi bulk odds response")
                except Exception as exc:
                    dumped_responses.append(
                        {
                            "bookmaker": provider_slug,
                            "chunk": chunk_index,
                            "tournament_ids": tournament_chunk,
                            "status": "failed",
                            "error": sanitized_error(exc),
                        }
                    )
                    logger.warning(
                        "oddspapi.bulk.chunk.failed bookmaker=%s chunk=%d/%d "
                        "tournament_ids=%s error_type=%s provider_error=%r",
                        provider_slug,
                        chunk_index,
                        len(tournament_chunks),
                        ",".join(tournament_chunk),
                        type(exc).__name__,
                        sanitized_error(exc),
                    )
                    continue
                dumped_responses.append(
                    {
                        "bookmaker": provider_slug,
                        "chunk": chunk_index,
                        "tournament_ids": tournament_chunk,
                        "status": "success",
                        "response": payload,
                    }
                )

                logger.info(
                    "oddspapi.bulk.response bookmaker=%s chunk=%d/%d fixtures=%d "
                    "requested_events=%d",
                    provider_slug,
                    chunk_index,
                    len(tournament_chunks),
                    len(fixture_payloads),
                    len(requested_event_ids),
                )
                bookmaker_fixture_count += len(fixture_payloads)
                for fixture_payload in fixture_payloads:
                    fixture_id = str(fixture_payload.get("fixtureId", ""))
                    bookmaker_odds = fixture_payload.get("bookmakerOdds")
                    bookmaker_count = len(bookmaker_odds) if isinstance(bookmaker_odds, dict) else 0
                    requested = fixture_id in requested_event_ids
                    logger.info(
                        "oddspapi.bulk.fixture bookmaker=%s chunk=%d fixture_id=%s "
                        "tournament_id=%s requested=%s bookmakers=%d start_time=%s",
                        provider_slug,
                        chunk_index,
                        fixture_id,
                        fixture_payload.get("tournamentId"),
                        requested,
                        bookmaker_count,
                        fixture_payload.get("startTime"),
                    )
                    if requested:
                        records = self._parse_event_odds(fixture_id, fixture_payload)
                        result[fixture_id].extend(records)
                        bookmaker_quote_count += len(records)
            logger.info(
                "oddspapi.bulk.bookmaker.complete bookmaker=%s fixtures=%d quotes=%d",
                provider_slug,
                bookmaker_fixture_count,
                bookmaker_quote_count,
            )
        await self._dump_bulk_odds(tournament_ids, dumped_responses)
        logger.info(
            "oddspapi.bulk.records.complete requested_fixtures=%d quotes=%d",
            len(result),
            sum(len(records) for records in result.values()),
        )
        return result

    def _parse_event_odds(
        self, event_id: str, payload: dict[str, Any]
    ) -> list[ProviderSportsbookQuote]:
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
                        bookmaker_outcome_id = (
                            str(raw["bookmakerOutcomeId"])
                            if raw.get("bookmakerOutcomeId") is not None
                            else None
                        )
                        if not bookmaker_outcome_agrees(selection, bookmaker_outcome_id):
                            logger.warning(
                                "oddspapi.outcome_semantics.rejected event_id=%s "
                                "bookmaker=%s market_id=%s outcome_id=%s selection=%s",
                                event_id,
                                bookmaker,
                                market_id,
                                outcome_id,
                                selection,
                            )
                            continue
                        record = ProviderSportsbookQuote(
                            provider_event_id=event_id,
                            bookmaker_id=bookmaker,
                            provider_outcome_id=int(outcome_id),
                            bookmaker_outcome_id=bookmaker_outcome_id,
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
                        records.append(record)
                        logger.info(
                            "oddspapi.odds.record fixture_id=%s bookmaker=%s "
                            "market_id=%s outcome_id=%s market_type=%s selection=%s "
                            "period=%s decimal_odds=%s active=%s changed_at=%s",
                            record.provider_event_id,
                            record.bookmaker_id,
                            record.market_id,
                            record.provider_outcome_id,
                            record.market_type,
                            record.selection,
                            record.period,
                            record.decimal_odds,
                            record.active and record.market_active,
                            record.changed_at.isoformat(),
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
