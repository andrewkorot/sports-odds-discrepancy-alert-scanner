from decimal import Decimal


def executable_yes_quote(
    *, best_bid: Decimal, best_ask: Decimal, bid_size: Decimal, ask_size: Decimal
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if best_ask < best_bid:
        raise ValueError("crossed order book")
    return best_bid, best_ask, bid_size, ask_size
