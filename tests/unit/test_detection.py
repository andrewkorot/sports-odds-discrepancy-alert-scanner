from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.domain.enums import AvailabilityStatus, MarketStatus
from app.domain.models import Opportunity
from app.providers.mock.data import mock_snapshot
from app.services.opportunity_detector import detect_opportunities

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
    prediction = predictions[0].model_copy(
        update={"best_ask_probability": sportsbooks[0].implied_probability - Decimal("0.03")}
    )
    result = detect_opportunities(
        [prediction], [sportsbooks[0]], [books[0]], Settings(edge_threshold_pp=Decimal("3")), NOW
    )
    assert len(result) == 1
    assert result[0].edge_percentage_points == Decimal("3")


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
