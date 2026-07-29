from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domain.enums import Provider
from app.providers.base import PredictionMarketConnector
from app.providers.kalshi.connector import KalshiConnector, KalshiRequestSigner
from app.providers.oddspapi.connector import SportsOddsConnector
from app.providers.polymarket.connector import PolymarketConnector
from app.providers.records import ProviderEvent, ProviderHealthRecord
from app.services.alerting import AlertCoordinator
from app.services.scanner import ScannerState


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
        self.sports_connector: SportsConnector | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._health: dict[Provider, ProviderHealthRecord] = {}
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
        await self.scan_once()
        self._task = asyncio.create_task(self._run(), name="scanner-poll-loop")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.price_poll_interval_seconds
                )
            except TimeoutError:
                await self.scan_once()

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
        operations: list[tuple[Provider, SportsConnector]] = [
            (connector_provider(connector), connector) for connector in self.prediction_connectors
        ]
        if self.sports_connector is not None:
            operations.append((Provider.ODDSPAPI, self.sports_connector))
        for provider, connector in operations:
            try:
                await connector.discover_events(start, end)
            except Exception:  # Health is sanitized by each connector; isolate the provider.
                pass
            self._health[provider] = await connector.health()

    def _discovery_window(self) -> tuple[datetime, datetime]:
        now = self.scanner.clock.now()
        local = now.astimezone(ZoneInfo(self.settings.client_timezone))
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    async def health(self) -> list[ProviderHealthRecord]:
        return list(self._health.values())

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


def connector_provider(connector: PredictionMarketConnector) -> Provider:
    if isinstance(connector, KalshiConnector):
        return Provider.KALSHI
    return Provider.POLYMARKET
