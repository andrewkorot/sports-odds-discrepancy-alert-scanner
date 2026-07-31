from decimal import Decimal


def decimal_odds_to_implied_probability(decimal_odds: Decimal) -> Decimal:
    if decimal_odds <= Decimal("1"):
        raise ValueError("decimal odds must be greater than 1")
    return Decimal("1") / decimal_odds


def calculate_edge_percentage_points(
    executable_yes_ask: Decimal, sportsbook_implied_probability: Decimal
) -> Decimal:
    return (executable_yes_ask - sportsbook_implied_probability) * Decimal("100")
