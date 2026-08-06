from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.domain.models import Opportunity, PredictionMarketQuote, SportsbookQuote
from app.providers.mock.data import mock_snapshot
from app.services.alert_deduplication import MemoryAlertDeduplicator
from app.services.alert_formatter import (
    FanoutTelegramSender,
    MockTelegramSender,
    format_telegram_alert,
)
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
        Decimal("4.5"),
    )
    assert "SOCCER PRICE DISCREPANCY" in message
    assert "Executable YES ask" in message
    assert "PREDICTION MARKETS" in message
    assert "SPORTSBOOKS" in message
    assert item.bookmaker_display_name in message
    assert " PST" in message
    assert " UTC" not in message
    assert "Depth within 4.5 cents of midpoint" in message
    assert "Pregame timing: PASSED" in message
    assert f"Value side: Sportsbook — {item.bookmaker_display_name}" in message
    assert (
        "Reference probability: Prediction market — "
        f"{item.prediction_market_provider.value.title()} "
        f"({item.prediction_market_best_ask:.1%})"
    ) in message
    assert "this is not a signal to buy the prediction-market YES contract" in message
    assert "QUALIFYING PAIR" in message
    assert "BEST OPPORTUNITY" not in message
    assert "this alert pair" in message
    assert "largest edge" in message


def test_telegram_deduplicates_quotes_and_identifies_largest_sportsbook_edge() -> None:
    item, predictions, sportsbooks = opportunity()
    base_prediction = next(
        quote
        for quote in predictions
        if quote.provider == item.prediction_market_provider
        and quote.provider_market_id == item.prediction_market_id
    )
    prediction = base_prediction.model_copy(
        update={
            "best_bid_probability": Decimal("0.61"),
            "best_ask_probability": Decimal("0.62"),
        }
    )
    base_sportsbook = next(
        quote
        for quote in sportsbooks
        if quote.canonical_event_id == item.canonical_event_id
        and quote.market_type == item.market_type
        and quote.selection == item.selection
        and quote.line == item.line
        and quote.participant == item.participant
    )
    alert_pair = base_sportsbook.model_copy(
        update={
            "bookmaker_id": "leovegas",
            "bookmaker_display_name": "LeoVegas",
            "decimal_odds": Decimal("1.64"),
            "implied_probability": Decimal("1") / Decimal("1.64"),
        }
    )
    better = base_sportsbook.model_copy(
        update={
            "bookmaker_id": "betus",
            "bookmaker_display_name": "BetUS",
            "decimal_odds": Decimal("1.77"),
            "implied_probability": Decimal("1") / Decimal("1.77"),
        }
    )
    item = item.model_copy(
        update={
            "prediction_market_best_bid": Decimal("0.61"),
            "prediction_market_best_ask": Decimal("0.62"),
            "bookmaker_id": "leovegas",
            "bookmaker_display_name": "LeoVegas",
            "sportsbook_decimal_odds": Decimal("1.64"),
            "sportsbook_implied_probability": Decimal("1") / Decimal("1.64"),
            "edge_percentage_points": (
                Decimal("0.62") - Decimal("1") / Decimal("1.64")
            )
            * 100,
        }
    )
    duplicate_prediction = prediction.model_copy()

    message = format_telegram_alert(
        item,
        [prediction, duplicate_prediction],
        [alert_pair, better],
    )

    assert message.count("Kalshi\nBid:") == 1
    assert "BetUS: 1.77 → 56.50% — largest edge" in message
    assert f"{alert_pair.bookmaker_display_name}:" in message
    assert "— this alert pair" in message
    assert "Largest sportsbook edge for this prediction ask: BetUS" in message


@pytest.mark.asyncio
async def test_telegram_fanout_delivers_to_client_and_owner() -> None:
    client = MockTelegramSender()
    owner = MockTelegramSender()
    sender = FanoutTelegramSender([client, owner])

    await sender.send("alert")

    assert client.messages == ["alert"]
    assert owner.messages == ["alert"]


def test_dual_telegram_destinations_override_legacy_pair() -> None:
    settings = Settings(
        telegram_bot_token="legacy-token",
        telegram_chat_id="legacy-chat",
        telegram_client_bot_token="client-token",
        telegram_client_chat_id="client-chat",
        telegram_owner_bot_token="owner-token",
        telegram_owner_chat_id="owner-chat",
    )

    assert settings.telegram_destinations() == [
        ("client-token", "client-chat"),
        ("owner-token", "owner-chat"),
    ]
