from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.domain.enums import AvailabilityStatus, MarketStatus
from app.domain.models import Opportunity
from app.providers.mock.data import mock_snapshot
from app.services.opportunity_detector import detect_opportunities, evaluate_candidates

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def detected(
    settings: Settings | None = None,
    mutate_prediction: dict[str, object] | None = None,
    mutate_sportsbook: dict[str, object] | None = None,
    mutate_book: dict[str, object] | None = None,
) -> list[Opportunity]:
    _, predictions, sportsbooks, books = mock_snapshot(NOW)
    if mutate_prediction:
        predictions[0] = predictions[0].model_copy(update=mutate_prediction)
        predictions = predictions[:1]
    if mutate_sportsbook:
        sportsbooks[0] = sportsbooks[0].model_copy(update=mutate_sportsbook)
        sportsbooks = sportsbooks[:1]
    if mutate_book:
        books[0] = books[0].model_copy(update=mutate_book)
        books = books[:1]
    return detect_opportunities(predictions, sportsbooks, books, settings or Settings(), NOW)


def test_threshold_boundary_exactly_three_qualifies() -> None:
    _, predictions, sportsbooks, books = mock_snapshot(NOW)
    ask = sportsbooks[0].implied_probability + Decimal("0.03")
    prediction = predictions[0].model_copy(
        update={
            "best_bid_probability": ask - Decimal("0.01"),
            "best_ask_probability": ask,
        }
    )
    result = detect_opportunities(
        [prediction], [sportsbooks[0]], [books[0]], Settings(edge_threshold_pp=Decimal("3")), NOW
    )
    assert len(result) == 1
    assert result[0].edge_percentage_points == Decimal("3")


def test_wrong_direction_is_explicitly_rejected() -> None:
    _, predictions, sportsbooks, books = mock_snapshot(NOW)
    prediction = predictions[0].model_copy(update={"best_ask_probability": Decimal("0.60")})
    sportsbook = sportsbooks[0].model_copy(
        update={
            "decimal_odds": Decimal("1.56"),
            "implied_probability": Decimal("1") / Decimal("1.56"),
        }
    )

    candidates = evaluate_candidates(
        [prediction],
        [sportsbook],
        [books[0]],
        Settings(edge_threshold_pp=Decimal("3")),
        NOW,
    )

    assert not candidates[0].accepted
    assert "prediction_probability_not_higher" in candidates[0].rejection_reasons
    assert candidates[0].edge_percentage_points.quantize(Decimal("0.01")) == Decimal("-4.10")


def test_client_alert_example_selects_pinnacle_and_rejects_stake() -> None:
    _, predictions, sportsbooks, books = mock_snapshot(NOW)
    prediction = predictions[0].model_copy(
        update={
            "best_bid_probability": Decimal("0.59"),
            "best_ask_probability": Decimal("0.60"),
        }
    )
    same_selection = [
        quote
        for quote in sportsbooks
        if quote.market_type == prediction.market_type
        and quote.selection == prediction.selection
        and quote.line == prediction.line
        and quote.participant == prediction.participant
    ]
    stake = next(quote for quote in same_selection if quote.bookmaker_id == "stake").model_copy(
        update={
            "decimal_odds": Decimal("1.56"),
            "implied_probability": Decimal("1") / Decimal("1.56"),
        }
    )
    pinnacle = next(
        quote for quote in same_selection if quote.bookmaker_id == "pinnacle"
    ).model_copy(
        update={
            "decimal_odds": Decimal("3.03"),
            "implied_probability": Decimal("1") / Decimal("3.03"),
        }
    )

    results = detect_opportunities(
        [prediction],
        [stake, pinnacle],
        books,
        Settings(edge_threshold_pp=Decimal("3")),
        NOW,
    )

    assert [result.bookmaker_id for result in results] == ["pinnacle"]
    assert results[0].edge_percentage_points.quantize(Decimal("0.01")) == Decimal("27.00")


def test_edge_below_threshold() -> None:
    assert not detected(Settings(edge_threshold_pp=Decimal("99")))


def test_stale_prediction_and_sportsbook_rejected() -> None:
    assert not detected(mutate_prediction={"source_timestamp": NOW - timedelta(seconds=21)})
    assert not detected(mutate_sportsbook={"source_timestamp": NOW - timedelta(seconds=61)})


def test_liquidity_disabled_and_live_rejected() -> None:
    assert not detected(mutate_prediction={"best_ask_size": Decimal("99")})
    assert not detected(mutate_book={"enabled": False})
    assert not detected(mutate_prediction={"market_status": MarketStatus.LIVE})


def test_missing_bookmaker_has_no_fabricated_quote() -> None:
    _, predictions, sportsbooks, books = mock_snapshot(NOW)
    sportsbooks = [q for q in sportsbooks if q.bookmaker_id != "coolbet"]
    results = detect_opportunities(predictions, sportsbooks, books, Settings(), NOW)
    assert all(result.bookmaker_id != "coolbet" for result in results)


def test_unavailable_bookmaker_rejected() -> None:
    assert not detected(mutate_book={"availability_status": AvailabilityStatus.UNAVAILABLE})


def test_prices_are_not_averaged() -> None:
    results = detected()
    assert len(results) == 108
    assert {r.prediction_market_provider for r in results} == {"kalshi", "polymarket"}
    assert len({r.bookmaker_id for r in results}) == 6
    kalshi_pinnacle = next(
        r
        for r in results
        if r.prediction_market_provider == "kalshi"
        and r.bookmaker_id == "pinnacle"
        and r.market_type == "moneyline"
        and r.selection == "home"
    )
    assert kalshi_pinnacle.prediction_market_best_ask == Decimal("0.520")
