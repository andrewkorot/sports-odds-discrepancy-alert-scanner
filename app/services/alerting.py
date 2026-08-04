from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.core.config import Settings
from app.domain.models import Opportunity
from app.services.alert_deduplication import MemoryAlertDeduplicator
from app.services.alert_formatter import (
    FanoutTelegramSender,
    TelegramHttpSender,
    TelegramSender,
    format_telegram_alert,
)
from app.services.scanner import ScannerState


class AlertDeduplicator(Protocol):
    async def should_alert(
        self, opportunity: Opportunity, cooldown: timedelta, edge_increase: Decimal
    ) -> bool: ...
    async def sync_active(self, opportunities: list[Opportunity]) -> None: ...
    async def aclose(self) -> None: ...


class AlertHistory(Protocol):
    async def record_alert(
        self, opportunity: Opportunity, sent_at: datetime, delivery_status: str
    ) -> None: ...


class AlertCoordinator:
    """Output-only alert gate. Dry-run and disabled flags short-circuit delivery."""

    def __init__(
        self,
        settings: Settings,
        scanner: ScannerState,
        sender: TelegramSender | None = None,
        deduplicator: AlertDeduplicator | None = None,
        history: AlertHistory | None = None,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.deduplicator = deduplicator or MemoryAlertDeduplicator()
        self.history = history
        self.sender = sender
        self._http_senders: list[TelegramHttpSender] = []
        if sender is None and self.delivery_enabled:
            self._http_senders = [
                TelegramHttpSender(token, chat_id)
                for token, chat_id in settings.telegram_destinations()
            ]
            self.sender = FanoutTelegramSender(self._http_senders)

    @property
    def delivery_enabled(self) -> bool:
        return not self.settings.live_dry_run and self.settings.alerts_enabled

    async def process(self) -> int:
        await self.deduplicator.sync_active(self.scanner.opportunities)
        if not self.delivery_enabled or self.sender is None:
            return 0
        sent = 0
        best_by_market: dict[tuple[object, ...], Opportunity] = {}
        for opportunity in self.scanner.opportunities:
            key = (
                opportunity.canonical_event_id,
                opportunity.market_type,
                opportunity.selection,
                opportunity.line,
                opportunity.participant,
            )
            previous = best_by_market.get(key)
            if (
                previous is None
                or opportunity.edge_percentage_points > previous.edge_percentage_points
            ):
                best_by_market[key] = opportunity
        for opportunity in best_by_market.values():
            if await self.deduplicator.should_alert(
                opportunity,
                timedelta(minutes=self.settings.alert_cooldown_minutes),
                self.settings.realert_edge_increase_pp,
            ):
                message = format_telegram_alert(
                    opportunity,
                    self.scanner.predictions,
                    self.scanner.sportsbooks,
                    self.settings.client_timezone,
                    self.settings.depth_window_from_midpoint_cents,
                )
                try:
                    await self.sender.send(message)
                except Exception:
                    if self.history is not None:
                        await self.history.record_alert(
                            opportunity, opportunity.detected_at, "failed"
                        )
                    continue
                if self.history is not None:
                    await self.history.record_alert(opportunity, opportunity.detected_at, "sent")
                sent += 1
        return sent

    async def aclose(self) -> None:
        for sender in self._http_senders:
            await sender.aclose()
        await self.deduplicator.aclose()
