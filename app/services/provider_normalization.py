from datetime import datetime
from decimal import Decimal

from app.domain.enums import Selection, VolumeSource
from app.domain.models import (
    NormalizedTrade,
    OrderBookLevel,
    OrderBookSnapshot,
)
from app.providers.records import ProviderBookLevel, ProviderOrderBook, ProviderTrade
from app.services.liquidity import make_level


def normalize_order_book(
    book: ProviderOrderBook,
    outcome: Selection,
    received_at: datetime,
    trailing_volume_usd: Decimal | None = None,
    volume_source: VolumeSource | None = None,
) -> OrderBookSnapshot:
    bids = _consolidate_levels(book.bids)
    asks = _consolidate_levels(book.asks)
    best_bid = max((level.price for level in bids), default=None)
    best_ask = min((level.price for level in asks), default=None)
    midpoint = (
        (best_bid + best_ask) / Decimal("2")
        if best_bid is not None and best_ask is not None
        else None
    )
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return OrderBookSnapshot(
        provider=book.provider,
        provider_market_id=book.provider_market_id,
        outcome=outcome,
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        spread_cents=spread * Decimal("100") if spread is not None else None,
        source_timestamp=book.source_timestamp,
        received_timestamp=received_at,
        trailing_24h_volume_usd=trailing_volume_usd,
        volume_source=volume_source,
    )


def _consolidate_levels(levels: list[ProviderBookLevel]) -> list[OrderBookLevel]:
    """Merge repeated provider price levels before qualification and persistence."""

    quantities: dict[Decimal, Decimal] = {}
    for raw_level in levels:
        price = raw_level.price
        quantities[price] = quantities.get(price, Decimal()) + raw_level.quantity
    return [make_level(price, quantity) for price, quantity in quantities.items()]


def normalize_trade(trade: ProviderTrade, selection_id: str) -> NormalizedTrade:
    return NormalizedTrade(
        provider=trade.provider,
        provider_market_id=trade.provider_market_id,
        selection_id=selection_id,
        price=trade.price,
        quantity=trade.quantity,
        notional_usd=trade.price * trade.quantity,
        executed_at=trade.executed_at,
    )
