from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.domain.models import Opportunity, PredictionMarketQuote, SportsbookQuote
from app.providers.mock.data import mock_snapshot
from app.services.alert_deduplication import MemoryAlertDeduplicator
from app.services.alert_formatter import format_telegram_alert
from app.services.opportunity_detector import detect_opportunities


def opportunity() -> tuple[Opportunity, list[PredictionMarketQuote], list[SportsbookQuote]]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _, predictions, sportsbooks, books = mock_snapshot(now)
    return (
        detect_opportunities(predictions, sportsbooks, books, Settings(), now)[0],
        predictions,
        sportsbooks,
    )


@pytest.mark.asyncio
async def test_duplicate_suppression_and_realert() -> None:
    item, _, _ = opportunity()
    dedupe = MemoryAlertDeduplicator()
    assert await dedupe.should_alert(item, timedelta(minutes=10), Decimal("1"))
    assert not await dedupe.should_alert(item, timedelta(minutes=10), Decimal("1"))
    improved = item.model_copy(
        update={"edge_percentage_points": item.edge_percentage_points + Decimal("1")}
    )
    assert await dedupe.should_alert(improved, timedelta(minutes=10), Decimal("1"))


def test_telegram_formatting() -> None:
    item, predictions, sportsbooks = opportunity()
    message = format_telegram_alert(
        item,
        predictions,
        sportsbooks,
        "America/Los_Angeles",
    )
    assert "SOCCER PRICE DISCREPANCY" in message
    assert "Executable YES ask" in message
    assert "PREDICTION MARKETS" in message
    assert "SPORTSBOOKS" in message
    assert item.bookmaker_display_name in message
    assert " PST" in message
    assert " UTC" not in message
