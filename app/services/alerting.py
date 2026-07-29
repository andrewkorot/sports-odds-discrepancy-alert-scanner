from datetime import timedelta

from app.core.config import Settings
from app.services.alert_deduplication import MemoryAlertDeduplicator
from app.services.alert_formatter import (
    TelegramHttpSender,
    TelegramSender,
    format_telegram_alert,
)
from app.services.scanner import ScannerState


class AlertCoordinator:
    """Output-only alert gate. Dry-run and disabled flags short-circuit delivery."""

    def __init__(
        self,
        settings: Settings,
        scanner: ScannerState,
        sender: TelegramSender | None = None,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.deduplicator = MemoryAlertDeduplicator()
        self.sender = sender
        self._http_sender: TelegramHttpSender | None = None
        if sender is None and self.delivery_enabled:
            assert settings.telegram_bot_token and settings.telegram_chat_id
            self._http_sender = TelegramHttpSender(
                settings.telegram_bot_token, settings.telegram_chat_id
            )
            self.sender = self._http_sender

    @property
    def delivery_enabled(self) -> bool:
        return (
            not self.settings.live_dry_run
            and self.settings.alerts_enabled
            and self.settings.telegram_enabled
        )

    async def process(self) -> int:
        if not self.delivery_enabled or self.sender is None:
            return 0
        sent = 0
        for opportunity in self.scanner.opportunities:
            if await self.deduplicator.should_alert(
                opportunity,
                timedelta(seconds=self.settings.alert_dedupe_ttl_seconds),
                self.settings.alert_edge_change_threshold * 100,
            ):
                message = format_telegram_alert(
                    opportunity, self.scanner.predictions, self.scanner.sportsbooks
                )
                await self.sender.send(message)
                sent += 1
        return sent

    async def aclose(self) -> None:
        if self._http_sender is not None:
            await self._http_sender.aclose()
