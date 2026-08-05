from datetime import UTC, datetime, time
from decimal import Decimal

from app.core.config import Settings
from app.services.alert_deduplication import deduplication_key
from app.services.alert_formatter import MockTelegramSender
from app.services.alerting import AlertCoordinator
from app.services.clock import FrozenClock
from app.services.orchestration import ScanOrchestrator
from app.services.scanner import ScannerState


class RuntimeSettingsRepository:
    def __init__(self) -> None:
        self.value: dict[str, object] | None = None

    async def load_system_setting(self, key: str) -> dict[str, object] | None:
        assert key == "runtime_settings"
        return self.value

    async def save_system_setting(
        self, key: str, value: dict[str, object], updated_at: datetime
    ) -> None:
        assert key == "runtime_settings"
        assert updated_at.tzinfo is not None
        self.value = value


async def test_dry_run_never_delivers_telegram() -> None:
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        live_dry_run=True,
        alerts_enabled=True,
    )
    scanner = ScannerState(settings)
    await scanner.refresh()
    sender = MockTelegramSender()
    coordinator = AlertCoordinator(settings, scanner, sender)
    assert await coordinator.process() == 0
    assert sender.messages == []


async def test_enabled_delivery_is_deduplicated() -> None:
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        live_dry_run=False,
        alerts_enabled=True,
        telegram_bot_token="test",
        telegram_chat_id="test",
    )
    scanner = ScannerState(settings)
    await scanner.refresh()
    sender = MockTelegramSender()
    coordinator = AlertCoordinator(settings, scanner, sender)
    expected_pairs = {deduplication_key(item) for item in scanner.opportunities}
    assert await coordinator.process() == len(expected_pairs)
    assert any("Prediction market: Kalshi" in message for message in sender.messages)
    assert any("Prediction market: Polymarket" in message for message in sender.messages)
    assert await coordinator.process() == 0


def test_discovery_window_uses_utc_calendar_days() -> None:
    now = datetime(2026, 7, 30, 16, 45, tzinfo=UTC)
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        kalshi_mode="mock",
        polymarket_mode="mock",
        sports_odds_mode="mock",
        client_timezone="America/Los_Angeles",
        discovery_calendar_days=3,
    )
    scanner = ScannerState(settings, FrozenClock(now))
    orchestrator = ScanOrchestrator(settings, scanner)

    start, end = orchestrator._discovery_window()

    assert start == datetime(2026, 7, 30, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 2, 0, tzinfo=UTC)


def test_discovery_window_is_not_affected_by_client_timezone_dst() -> None:
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        kalshi_mode="mock",
        polymarket_mode="mock",
        sports_odds_mode="mock",
        client_timezone="America/Los_Angeles",
        discovery_calendar_days=1,
    )
    scanner = ScannerState(settings, FrozenClock(now))
    orchestrator = ScanOrchestrator(settings, scanner)

    start, end = orchestrator._discovery_window()

    assert start == datetime(2026, 11, 1, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 0, tzinfo=UTC)


def test_daily_scan_schedule_uses_client_timezone() -> None:
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        auto_start_stop_enabled=True,
        scan_auto_start_time=time(6),
        scan_auto_stop_time=time(23),
        client_timezone="America/Los_Angeles",
    )
    scanner = ScannerState(settings, FrozenClock(datetime(2026, 8, 4, 14, tzinfo=UTC)))
    orchestrator = ScanOrchestrator(settings, scanner)

    assert orchestrator.schedule_active_at(datetime(2026, 8, 4, 14, tzinfo=UTC))
    assert not orchestrator.schedule_active_at(datetime(2026, 8, 4, 8, tzinfo=UTC))


def test_overnight_scan_schedule_is_supported() -> None:
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        auto_start_stop_enabled=True,
        scan_auto_start_time=time(20),
        scan_auto_stop_time=time(6),
        client_timezone="America/Los_Angeles",
    )
    scanner = ScannerState(settings)
    orchestrator = ScanOrchestrator(settings, scanner)

    assert orchestrator.schedule_active_at(datetime(2026, 8, 4, 6, tzinfo=UTC))
    assert not orchestrator.schedule_active_at(datetime(2026, 8, 4, 18, tzinfo=UTC))


async def test_manual_stop_does_not_cancel_current_scan() -> None:
    settings = Settings(app_mode="mock", mock_mode=True)
    scanner = ScannerState(settings)
    orchestrator = ScanOrchestrator(settings, scanner)
    orchestrator.scan_in_progress = True

    await orchestrator.manual_stop()

    assert not orchestrator.scanning_enabled
    assert orchestrator.scan_in_progress
    assert orchestrator.scan_control_source == "manual"

    await orchestrator.manual_start()
    assert orchestrator.scanning_enabled


async def test_runtime_settings_apply_to_shared_scanner_configuration() -> None:
    settings = Settings(app_mode="mock", mock_mode=True)
    scanner = ScannerState(settings)
    orchestrator = ScanOrchestrator(settings, scanner)

    updated = await orchestrator.update_runtime_settings(
        {"price_poll_interval_seconds": 125, "edge_threshold_pp": Decimal("4.25")}
    )

    assert settings.price_poll_interval_seconds == 125
    assert scanner.settings.edge_threshold_pp == Decimal("4.25")
    assert updated["edge_threshold_pp"] == Decimal("4.25")


async def test_runtime_settings_persist_in_postgresql_repository_and_reload() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    settings = Settings(app_mode="mock", mock_mode=True)
    repository = RuntimeSettingsRepository()
    orchestrator = ScanOrchestrator(settings, ScannerState(settings, FrozenClock(now)))
    orchestrator.repository = repository  # type: ignore[assignment]

    await orchestrator.update_runtime_settings(
        {"price_poll_interval_seconds": 180, "edge_threshold_pp": Decimal("4.5")}
    )

    assert repository.value is not None
    assert repository.value["price_poll_interval_seconds"] == 180
    restored_settings = Settings(app_mode="mock", mock_mode=True)
    restored = ScanOrchestrator(
        restored_settings,
        ScannerState(restored_settings, FrozenClock(now)),
    )
    restored.repository = repository  # type: ignore[assignment]
    await restored._load_runtime_settings()
    assert restored_settings.price_poll_interval_seconds == 180
    assert restored_settings.edge_threshold_pp == Decimal("4.5")
