from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from time import perf_counter
from typing import Protocol
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import RUNTIME_SETTING_KEYS, Settings
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
SCAN_CONTROL_REDIS_KEY = "scanner:run-control"
RUNTIME_SETTINGS_REDIS_KEY = "scanner:runtime-settings"


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
        self._control_changed = asyncio.Event()
        self._health: dict[Provider, ProviderHealthRecord] = {}
        self.last_scan_error: str | None = None
        self.scan_in_progress = False
        self.scanning_enabled = True
        self.scan_control_source = "startup"
        self._schedule_phase: bool | None = None
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
        await self._load_runtime_settings()
        await self._initialize_run_control()
        self._task = asyncio.create_task(self._run(), name="scanner-poll-loop")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self._apply_schedule_transition()
            if self.scanning_enabled:
                self.scan_in_progress = True
                try:
                    await self.scan_once()
                finally:
                    self.scan_in_progress = False
            delay = float(
                self.settings.price_poll_interval_seconds
                if self.scanning_enabled
                else min(self.settings.price_poll_interval_seconds, 30)
            )
            if self.settings.auto_start_stop_enabled:
                delay = min(delay, self._seconds_until_schedule_boundary(self.scanner.clock.now()))
            try:
                await asyncio.wait_for(self._control_changed.wait(), timeout=delay)
            except TimeoutError:
                pass
            finally:
                self._control_changed.clear()

    def schedule_active_at(self, moment: datetime) -> bool:
        """Return whether a local wall-clock time falls inside the daily scan window."""
        local_time = (
            moment.astimezone(ZoneInfo(self.settings.client_timezone)).time().replace(tzinfo=None)
        )
        start = self.settings.scan_auto_start_time
        stop = self.settings.scan_auto_stop_time
        if start < stop:
            return start <= local_time < stop
        return local_time >= start or local_time < stop

    def _seconds_until_schedule_boundary(self, moment: datetime) -> float:
        zone = ZoneInfo(self.settings.client_timezone)
        local_now = moment.astimezone(zone)
        boundary_time = (
            self.settings.scan_auto_stop_time
            if self.schedule_active_at(moment)
            else self.settings.scan_auto_start_time
        )
        boundary = datetime.combine(local_now.date(), boundary_time, tzinfo=zone)
        if boundary <= local_now:
            boundary += timedelta(days=1)
        return max(0.1, (boundary.astimezone(UTC) - moment.astimezone(UTC)).total_seconds())

    async def _initialize_run_control(self) -> None:
        current_phase = self.schedule_active_at(self.scanner.clock.now())
        self._schedule_phase = current_phase
        restored = await self._load_run_control()
        if restored is not None:
            enabled, stored_phase = restored
            if not self.settings.auto_start_stop_enabled or stored_phase == current_phase:
                self.scanning_enabled = enabled
                self.scan_control_source = "restored"
                return
        if self.settings.auto_start_stop_enabled:
            self.scanning_enabled = current_phase
            self.scan_control_source = "schedule"
        await self._persist_run_control()

    async def _apply_schedule_transition(self) -> None:
        if not self.settings.auto_start_stop_enabled:
            return
        current_phase = self.schedule_active_at(self.scanner.clock.now())
        if self._schedule_phase == current_phase:
            return
        self._schedule_phase = current_phase
        self.scanning_enabled = current_phase
        self.scan_control_source = "schedule"
        await self._persist_run_control()
        logger.info("scanner.schedule.%s", "started" if current_phase else "stopped")

    async def manual_start(self) -> None:
        self.scanning_enabled = True
        self.scan_control_source = "manual"
        await self._persist_run_control()
        self._control_changed.set()

    async def manual_stop(self) -> None:
        """Prevent another scan from starting without cancelling the current scan."""
        self.scanning_enabled = False
        self.scan_control_source = "manual"
        await self._persist_run_control()
        self._control_changed.set()

    async def update_runtime_settings(self, updates: dict[str, object]) -> dict[str, object]:
        unknown = set(updates) - RUNTIME_SETTING_KEYS
        if unknown:
            raise ValueError(f"Settings are not runtime-editable: {', '.join(sorted(unknown))}")
        validated = Settings.model_validate({**self.settings.model_dump(), **updates})
        for key in updates:
            setattr(self.settings, key, getattr(validated, key))
        await self._persist_runtime_settings()
        schedule_keys = {
            "auto_start_stop_enabled",
            "scan_auto_start_time",
            "scan_auto_stop_time",
        }
        if schedule_keys.intersection(updates):
            self._schedule_phase = None
            await self._apply_schedule_transition()
        self._control_changed.set()
        return self.runtime_settings()

    def runtime_settings(self) -> dict[str, object]:
        return {key: getattr(self.settings, key) for key in sorted(RUNTIME_SETTING_KEYS)}

    async def _load_runtime_settings(self) -> None:
        if self._redis is None:
            return
        try:
            raw = await self._redis.get(RUNTIME_SETTINGS_REDIS_KEY)
            if not raw:
                return
            values = json.loads(raw)
            if not isinstance(values, dict):
                return
            validated = Settings.model_validate({**self.settings.model_dump(), **values})
            for key in RUNTIME_SETTING_KEYS:
                if key in values:
                    setattr(self.settings, key, getattr(validated, key))
        except Exception:
            logger.warning("scanner.runtime_settings.load_failed")

    async def _persist_runtime_settings(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                RUNTIME_SETTINGS_REDIS_KEY,
                json.dumps(self.runtime_settings(), default=str),
            )
        except Exception:
            logger.warning("scanner.runtime_settings.persistence_failed")

    async def _load_run_control(self) -> tuple[bool, bool] | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(SCAN_CONTROL_REDIS_KEY)
            if not raw:
                return None
            value = json.loads(raw)
            return bool(value["enabled"]), bool(value["schedule_phase"])
        except Exception:
            return None

    async def _persist_run_control(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                SCAN_CONTROL_REDIS_KEY,
                json.dumps(
                    {"enabled": self.scanning_enabled, "schedule_phase": self._schedule_phase}
                ),
            )
        except Exception:
            logger.warning("scanner.run_control.persistence_failed")

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
        utc_date = self.scanner.clock.now().astimezone(UTC).date()
        utc_start = datetime.combine(utc_date, time.min, tzinfo=UTC)
        discovery_days = self.settings.discovery_calendar_days
        utc_end = datetime.combine(
            utc_date + timedelta(days=discovery_days),
            time.min,
            tzinfo=UTC,
        )
        return utc_start, utc_end

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
