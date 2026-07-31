from decimal import Decimal, getcontext

import pytest

from app.providers.kalshi.pricing import derive_yes_ask
from app.providers.polymarket.mapping import executable_yes_quote
from app.services.edge_calculator import (
    calculate_edge_percentage_points,
    decimal_odds_to_implied_probability,
)


def test_decimal_odds_conversion_and_precision() -> None:
    probability = decimal_odds_to_implied_probability(Decimal("2.15"))
    assert probability == Decimal("1") / Decimal("2.15")
    assert probability.quantize(Decimal("0.0001")) == Decimal("0.4651")
    assert getcontext().prec >= 28


def test_ask_based_edge_is_percentage_points() -> None:
    implied = decimal_odds_to_implied_probability(Decimal("2.15"))
    edge = calculate_edge_percentage_points(Decimal("0.52"), implied)
    assert edge.quantize(Decimal("0.0001")) == Decimal("5.4884")


def test_client_example_rejects_sportsbook_probability_above_prediction_ask() -> None:
    implied = decimal_odds_to_implied_probability(Decimal("1.56"))
    edge = calculate_edge_percentage_points(Decimal("0.60"), implied)
    assert implied.quantize(Decimal("0.0001")) == Decimal("0.6410")
    assert edge.quantize(Decimal("0.01")) == Decimal("-4.10")


def test_client_example_accepts_better_sportsbook_price() -> None:
    implied = decimal_odds_to_implied_probability(Decimal("1.78"))
    edge = calculate_edge_percentage_points(Decimal("0.60"), implied)
    assert implied.quantize(Decimal("0.0001")) == Decimal("0.5618")
    assert edge.quantize(Decimal("0.01")) == Decimal("3.82")


def test_kalshi_derived_ask_and_size() -> None:
    ask, size = derive_yes_ask(Decimal("0.48"), Decimal("850"))
    assert ask == Decimal("0.52")
    assert size == Decimal("850")


def test_polymarket_executable_ask_preserves_book() -> None:
    assert executable_yes_quote(
        best_bid=Decimal("0.507"),
        best_ask=Decimal("0.508"),
        bid_size=Decimal("4100"),
        ask_size=Decimal("3400"),
    ) == (Decimal("0.507"), Decimal("0.508"), Decimal("4100"), Decimal("3400"))


def test_invalid_decimal_odds() -> None:
    with pytest.raises(ValueError):
        decimal_odds_to_implied_probability(Decimal("1"))
