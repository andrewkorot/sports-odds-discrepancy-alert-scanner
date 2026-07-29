from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.models import Opportunity


def deduplication_key(opportunity: Opportunity) -> str:
    return ":".join(
        [
            str(opportunity.canonical_event_id),
            opportunity.market_type,
            opportunity.selection,
            opportunity.prediction_market_provider,
            opportunity.prediction_market_id,
            opportunity.bookmaker_id,
        ]
    )


@dataclass
class AlertState:
    sent_at: datetime
    edge: Decimal
    active: bool = True


class MemoryAlertDeduplicator:
    """Deterministic stand-in for the Redis implementation used by tests/mock mode."""

    def __init__(self) -> None:
        self._states: dict[str, AlertState] = {}

    async def should_alert(
        self,
        opportunity: Opportunity,
        cooldown: timedelta,
        edge_increase: Decimal,
    ) -> bool:
        key = deduplication_key(opportunity)
        previous = self._states.get(key)
        allowed = (
            previous is None
            or not previous.active
            or opportunity.detected_at - previous.sent_at >= cooldown
            or opportunity.edge_percentage_points - previous.edge >= edge_increase
        )
        if allowed:
            self._states[key] = AlertState(
                opportunity.detected_at, opportunity.edge_percentage_points
            )
        return allowed

    async def mark_disappeared(self, key: str) -> None:
        if key in self._states:
            self._states[key].active = False
