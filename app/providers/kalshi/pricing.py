from decimal import Decimal


def derive_yes_ask(best_no_bid: Decimal, no_bid_size: Decimal) -> tuple[Decimal, Decimal]:
    """A YES taker crosses the economically equivalent resting NO bid."""
    if not Decimal("0") <= best_no_bid <= Decimal("1"):
        raise ValueError("NO bid must be within [0, 1]")
    if no_bid_size < 0:
        raise ValueError("size cannot be negative")
    return Decimal("1") - best_no_bid, no_bid_size
