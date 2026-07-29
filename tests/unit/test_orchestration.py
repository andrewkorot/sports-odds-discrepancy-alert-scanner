from app.core.config import Settings
from app.services.alert_formatter import MockTelegramSender
from app.services.alerting import AlertCoordinator
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
    assert await coordinator.process() == len(scanner.opportunities)
    assert await coordinator.process() == 0
