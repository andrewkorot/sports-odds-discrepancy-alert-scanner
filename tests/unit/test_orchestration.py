from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.services.alert_formatter import MockTelegramSender
from app.services.alerting import AlertCoordinator
from app.services.clock import FrozenClock
from app.services.orchestration import ScanOrchestrator
from app.services.scanner import ScannerState


async def test_dry_run_never_delivers_telegram() -> None:
    settings = Settings(live_dry_run=True, alerts_enabled=True, telegram_enabled=True)
    scanner = ScannerState(settings)
    await scanner.refresh()
    sender = MockTelegramSender()
    coordinator = AlertCoordinator(settings, scanner, sender)
    assert await coordinator.process() == 0
    assert sender.messages == []


async def test_enabled_delivery_is_deduplicated() -> None:
    settings = Settings(
        live_dry_run=False,
        alerts_enabled=True,
        telegram_enabled=True,
        telegram_bot_token="test",
        telegram_chat_id="test",
    )
    scanner = ScannerState(settings)
    await scanner.refresh()
    sender = MockTelegramSender()
    coordinator = AlertCoordinator(settings, scanner, sender)
    expected_markets = {
        (
            item.canonical_event_id,
            item.market_type,
            item.selection,
            item.line,
            item.participant,
        )
        for item in scanner.opportunities
    }
    assert await coordinator.process() == len(expected_markets)
    assert await coordinator.process() == 0


def test_discovery_window_uses_configured_maximum_hours() -> None:
    now = datetime(2026, 7, 30, 16, 45, tzinfo=UTC)
    settings = Settings(
        app_mode="mock",
        mock_mode=True,
        kalshi_mode="mock",
        polymarket_mode="mock",
        sports_odds_mode="mock",
        max_hours_before_kickoff=72,
    )
    scanner = ScannerState(settings, FrozenClock(now))
    orchestrator = ScanOrchestrator(settings, scanner)

    start, end = orchestrator._discovery_window()

    assert start == now
    assert end == now + timedelta(hours=72)
