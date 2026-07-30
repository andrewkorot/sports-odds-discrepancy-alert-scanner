from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.session import create_engine_and_session
from app.domain.enums import Provider
from app.providers.base import PredictionMarketConnector
from app.providers.kalshi.connector import KalshiConnector, KalshiRequestSigner
from app.providers.oddspapi.connector import SportsOddsConnector
from app.providers.polymarket.connector import PolymarketConnector
from app.providers.records import ProviderEvent, ProviderHealthRecord
from app.repositories.live_scan import LiveScanRepository
from app.services.alert_deduplication import RedisAlertDeduplicator
from app.services.alerting import AlertCoordinator
from app.services.live_pipeline import collect_live_snapshot
from app.services.scanner import ScannerState

logger = logging.getLogger("uvicorn.error")


class SportsConnector(Protocol):
    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]: ...
    async def health(self) -> ProviderHealthRecord: ...
    async def aclose(self) -> None: ...


class ScanOrchestrator:
    """Owns recurring scans and isolates failures between read-only providers."""

    def __init__(self, settings: Settings, scanner: ScannerState) -> None:
        self.settings = settings
        self.scanner = scanner
        self.prediction_connectors: list[PredictionMarketConnector] = []
        self.sports_connector: SportsOddsConnector | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._health: dict[Provider, ProviderHealthRecord] = {}
        self.last_scan_error: str | None = None
        self.scan_in_progress = False
        self._engine: AsyncEngine | None = None
        self._redis: Redis | None = None
        self.repository: LiveScanRepository | None = None
        if settings.app_mode == "live" or not settings.mock_mode:
            self._engine, sessions = create_engine_and_session(settings.database_url)
            self.repository = LiveScanRepository(sessions)
            self._redis = Redis.from_url(settings.redis_url, decode_responses=True)
            self.alerts = AlertCoordinator(
                settings,
                scanner,
                deduplicator=RedisAlertDeduplicator(self._redis),
                history=self.repository,
            )
        else:
            self.alerts = AlertCoordinator(settings, scanner)
        self._configure()

    def _configure(self) -> None:
        if self.settings.kalshi_mode == "live":
            key_id = self.settings.kalshi_api_key_id or self.settings.kalshi_api_key
            assert key_id is not None and self.settings.kalshi_private_key_path is not None
            self.prediction_connectors.append(
                KalshiConnector(
                    self.settings.kalshi_base_url,
                    KalshiRequestSigner(key_id, self.settings.kalshi_private_key_path),
                )
            )
        elif self.settings.kalshi_mode == "disabled":
            self._health[Provider.KALSHI] = self._disabled(Provider.KALSHI)
        if self.settings.polymarket_mode == "live":
            self.prediction_connectors.append(
                PolymarketConnector(
                    self.settings.polymarket_gamma_base_url,
                    self.settings.polymarket_clob_base_url,
                    self.settings.polymarket_data_base_url,
                )
            )
        elif self.settings.polymarket_mode == "disabled":
            self._health[Provider.POLYMARKET] = self._disabled(Provider.POLYMARKET)
        if self.settings.sports_odds_mode == "live":
            api_key = self.settings.sports_odds_api_key or self.settings.oddspapi_api_key
            assert api_key is not None
            self.sports_connector = SportsOddsConnector(
                api_key,
                self.settings.sports_odds_base_url,
                self.settings.enabled_bookmakers,
                discovery_dump_path=self.settings.oddspapi_discovery_dump_path,
            )
        elif self.settings.sports_odds_mode == "disabled":
            self._health[Provider.ODDSPAPI] = self._disabled(Provider.ODDSPAPI)

    @staticmethod
    def _disabled(provider: Provider) -> ProviderHealthRecord:
        return ProviderHealthRecord(
            provider=provider,
            mode="disabled",
            enabled=False,
            connected=False,
        )

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scanner-poll-loop")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            self.scan_in_progress = True
            try:
                await self.scan_once()
            finally:
                self.scan_in_progress = False
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.price_poll_interval_seconds
                )
            except TimeoutError:
                continue

    async def scan_once(self) -> None:
        if self.settings.app_mode == "mock" and self.settings.mock_mode:
            await self.scanner.refresh()
            now = self.scanner.last_updated
            for provider in Provider:
                self._health[provider] = ProviderHealthRecord(
                    provider=provider,
                    mode="mock",
                    enabled=True,
                    connected=True,
                    last_attempt_at=now,
                    last_success_at=now,
                    events_discovered=len(self.scanner.events),
                    markets_discovered=len(self.scanner.predictions),
                    books_updated=len(self.scanner.predictions),
                )
            await self.alerts.process()
            return
        start, end = self._discovery_window()
        if self.sports_connector is not None:
            try:
                now = self.scanner.clock.now()
                approved_event_mappings = (
                    await self.repository.approved_event_mappings()
                    if self.repository is not None
                    else {}
                )
                snapshot = await collect_live_snapshot(
                    self.prediction_connectors,
                    self.sports_connector,
                    self.settings,
                    now,
                    start,
                    end,
                    approved_event_mappings,
                )
                if self.repository is not None:
                    logger.info("scan.persistence.start")
                    persistence_started = perf_counter()
                    await self.repository.persist(snapshot)
                    logger.info(
                        "scan.persistence.complete duration_seconds=%.3f",
                        perf_counter() - persistence_started,
                    )
                self.scanner.apply_live_snapshot(snapshot, now)
                await self.alerts.process()
                self.last_scan_error = None
            except Exception as exc:
                logger.exception("Live scan failed")
                self.last_scan_error = self._safe_scan_error(exc)
        operations: list[tuple[Provider, SportsConnector]] = [
            (connector_provider(connector), connector) for connector in self.prediction_connectors
        ]
        if self.sports_connector is not None:
            operations.append((Provider.ODDSPAPI, self.sports_connector))
        for provider, connector in operations:
            self._health[provider] = await connector.health()
        if self.repository is not None:
            try:
                await self.repository.persist_health(list(self._health.values()))
            except Exception:
                pass

    @staticmethod
    def _safe_scan_error(exc: Exception) -> str:
        if isinstance(exc, IntegrityError):
            original = getattr(exc, "orig", None)
            diagnostic = getattr(original, "diag", None)
            constraint = getattr(diagnostic, "constraint_name", None)
            if constraint:
                return f"Live scan failed: IntegrityError ({constraint})"
        return f"Live scan failed: {type(exc).__name__}"

    def _discovery_window(self) -> tuple[datetime, datetime]:
        now = self.scanner.clock.now().astimezone(UTC)
        return now, now + timedelta(hours=self.settings.max_hours_before_kickoff)

    async def health(self) -> list[ProviderHealthRecord]:
        return list(self._health.values())

    async def infrastructure_health(self) -> tuple[str, str]:
        if self.settings.mock_mode:
            return "mock", "mock"
        database_status = "unavailable"
        redis_status = "unavailable"
        if self._engine is not None:
            try:
                async with self._engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                database_status = "ok"
            except Exception:
                pass
        if self._redis is not None:
            try:
                if await self._redis.ping():
                    redis_status = "ok"
            except Exception:
                pass
        return database_status, redis_status

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        for connector in self.prediction_connectors:
            await connector.aclose()
        if self.sports_connector is not None:
            await self.sports_connector.aclose()
        await self.alerts.aclose()
        if self._engine is not None:
            await self._engine.dispose()


def connector_provider(connector: PredictionMarketConnector) -> Provider:
    if isinstance(connector, KalshiConnector):
        return Provider.KALSHI
    return Provider.POLYMARKET
