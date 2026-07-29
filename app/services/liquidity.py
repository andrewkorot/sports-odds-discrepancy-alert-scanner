from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from app.core.config import Settings
from app.domain.models import (
    LiquidityQualification,
    OrderBookLevel,
    OrderBookSnapshot,
)


class DepthResult(BaseModel):
    bid_usd: Decimal
    ask_usd: Decimal

    @property
    def total_usd(self) -> Decimal:
        return self.bid_usd + self.ask_usd


class DepthCalculationStrategy(Protocol):
    def calculate(
        self,
        snapshot: OrderBookSnapshot,
        midpoint: Decimal,
        window_probability: Decimal,
    ) -> DepthResult: ...


class CombinedTwoSidedDepth:
    def calculate(
        self,
        snapshot: OrderBookSnapshot,
        midpoint: Decimal,
        window_probability: Decimal,
    ) -> DepthResult:
        lower = midpoint - window_probability
        upper = midpoint + window_probability
        bid_levels = {
            (level.price, level.quantity): level
            for level in snapshot.bids
            if lower <= level.price <= midpoint
        }
        ask_levels = {
            (level.price, level.quantity): level
            for level in snapshot.asks
            if midpoint <= level.price <= upper
        }
        return DepthResult(
            bid_usd=sum((x.notional_usd for x in bid_levels.values()), Decimal()),
            ask_usd=sum((x.notional_usd for x in ask_levels.values()), Decimal()),
        )


def make_level(price: Decimal, quantity: Decimal) -> OrderBookLevel:
    return OrderBookLevel(price=price, quantity=quantity, notional_usd=price * quantity)


def qualify_liquidity(
    snapshot: OrderBookSnapshot,
    settings: Settings,
    strategy: DepthCalculationStrategy | None = None,
) -> LiquidityQualification:
    reasons: list[str] = []
    bid, ask = snapshot.best_bid, snapshot.best_ask
    midpoint: Decimal | None = None
    spread_cents: Decimal | None = None
    spread_passed = False
    if bid is None:
        reasons.append("missing_best_bid")
    if ask is None:
        reasons.append("missing_best_ask")
    if bid is not None and ask is not None:
        if not Decimal("0") <= bid <= Decimal("1") or not Decimal("0") <= ask <= Decimal("1"):
            reasons.append("invalid_order_book")
        elif ask <= bid:
            reasons.append("invalid_order_book")
        else:
            midpoint = (bid + ask) / Decimal("2")
            spread_cents = (ask - bid) * Decimal("100")
            spread_passed = spread_cents < settings.max_bid_ask_spread_cents
            if not spread_passed:
                reasons.append("spread_too_wide")
    depth = DepthResult(bid_usd=Decimal(), ask_usd=Decimal())
    if midpoint is not None:
        depth = (strategy or CombinedTwoSidedDepth()).calculate(
            snapshot,
            midpoint,
            settings.depth_window_from_midpoint_cents / Decimal("100"),
        )
    depth_passed = depth.total_usd >= settings.min_depth_within_window_usd
    if not depth_passed:
        reasons.append("insufficient_depth")
    volume = snapshot.trailing_24h_volume_usd
    volume_passed = (
        volume is not None
        and snapshot.volume_source is not None
        and volume >= settings.min_trailing_24h_volume_usd
    )
    if volume is None or snapshot.volume_source is None:
        reasons.append("volume_unverified")
    elif not volume_passed:
        reasons.append("insufficient_24h_volume")
    return LiquidityQualification(
        best_bid=bid,
        best_ask=ask,
        midpoint=midpoint,
        spread_cents=spread_cents,
        maximum_spread_cents=settings.max_bid_ask_spread_cents,
        spread_passed=spread_passed,
        depth_window_cents=settings.depth_window_from_midpoint_cents,
        bid_depth_within_window_usd=depth.bid_usd,
        ask_depth_within_window_usd=depth.ask_usd,
        total_depth_within_window_usd=depth.total_usd,
        minimum_depth_usd=settings.min_depth_within_window_usd,
        depth_passed=depth_passed,
        trailing_24h_volume_usd=volume,
        minimum_trailing_24h_volume_usd=settings.min_trailing_24h_volume_usd,
        volume_source=snapshot.volume_source,
        volume_passed=volume_passed,
        overall_passed=not reasons,
        rejection_reasons=reasons,
    )


class Trade(BaseModel):
    execution_price: Decimal
    executed_quantity: Decimal
    executed_at: datetime


def calculate_trailing_volume(trades: list[Trade], now_utc: datetime) -> Decimal:
    boundary = now_utc - timedelta(hours=24)
    return sum(
        (
            trade.execution_price * trade.executed_quantity
            for trade in trades
            if boundary < trade.executed_at <= now_utc
        ),
        Decimal(),
    )
