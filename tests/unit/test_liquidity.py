from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.domain.enums import Provider, Selection, VolumeSource
from app.domain.models import OrderBookSnapshot
from app.services.liquidity import (
    CombinedTwoSidedDepth,
    Trade,
    calculate_trailing_volume,
    make_level,
    qualify_liquidity,
)

NOW = datetime(2026, 7, 29, 16, tzinfo=UTC)


def snapshot(
    bid: Decimal | None = Decimal("0.50"),
    ask: Decimal | None = Decimal("0.54"),
    volume: Decimal | None = Decimal("5000"),
    source: VolumeSource | None = VolumeSource.PROVIDER_REPORTED,
) -> OrderBookSnapshot:
    bids = (
        []
        if bid is None
        else [
            make_level(bid, Decimal("2000")),
            make_level(Decimal("0.49"), Decimal("1000")),
            make_level(Decimal("0.45"), Decimal("9999")),
        ]
    )
    asks = (
        []
        if ask is None
        else [
            make_level(ask, Decimal("1000")),
            make_level(Decimal("0.53"), Decimal("1000")),
            make_level(Decimal("0.60"), Decimal("9999")),
        ]
    )
    return OrderBookSnapshot(
        provider=Provider.KALSHI,
        provider_market_id="quality-test",
        outcome=Selection.HOME,
        bids=bids,
        asks=asks,
        best_bid=bid,
        best_ask=ask,
        midpoint=None,
        spread=None,
        spread_cents=None,
        source_timestamp=NOW,
        received_timestamp=NOW,
        trailing_24h_volume_usd=volume,
        volume_source=source,
    )


def test_spread_boundaries_and_midpoint() -> None:
    passing = qualify_liquidity(snapshot(), Settings(min_depth_within_window_usd=0))
    assert passing.midpoint == Decimal("0.52")
    assert passing.spread_cents == Decimal("4")
    assert passing.spread_passed
    exactly_five = qualify_liquidity(
        snapshot(ask=Decimal("0.55")), Settings(min_depth_within_window_usd=0)
    )
    assert not exactly_five.spread_passed
    assert "spread_too_wide" in exactly_five.rejection_reasons
    assert not qualify_liquidity(
        snapshot(ask=Decimal("0.56")), Settings(min_depth_within_window_usd=0)
    ).spread_passed


def test_missing_and_crossed_books_fail() -> None:
    assert "missing_best_bid" in qualify_liquidity(snapshot(bid=None), Settings()).rejection_reasons
    assert "missing_best_ask" in qualify_liquidity(snapshot(ask=None), Settings()).rejection_reasons
    crossed = qualify_liquidity(snapshot(bid=Decimal("0.55"), ask=Decimal("0.54")), Settings())
    assert "invalid_order_book" in crossed.rejection_reasons


def test_depth_window_notional_and_outside_exclusion() -> None:
    book = snapshot()
    result = CombinedTwoSidedDepth().calculate(book, Decimal("0.52"), Decimal("0.03"))
    assert result.bid_usd == Decimal("1490")  # .50*2000 + .49*1000
    assert result.ask_usd == Decimal("1070")  # .54*1000 + .53*1000
    assert result.total_usd == Decimal("2560")
    assert make_level(Decimal("0.52"), Decimal("850")).notional_usd == Decimal("442.00")


def test_depth_and_volume_inclusive_boundaries() -> None:
    quality = qualify_liquidity(
        snapshot(volume=Decimal("5000")),
        Settings(min_depth_within_window_usd=Decimal("2560")),
    )
    assert quality.depth_passed and quality.volume_passed
    below = qualify_liquidity(
        snapshot(volume=Decimal("4999.99")),
        Settings(min_depth_within_window_usd=Decimal("2560.01")),
    )
    assert {"insufficient_depth", "insufficient_24h_volume"} <= set(below.rejection_reasons)

    exact_book = snapshot()
    exact_book = exact_book.model_copy(
        update={
            "best_bid": Decimal("0.49"),
            "best_ask": Decimal("0.51"),
            "bids": [make_level(Decimal("0.49"), Decimal("2000"))],
            "asks": [make_level(Decimal("0.51"), Decimal("2000"))],
        }
    )
    exact = qualify_liquidity(exact_book, Settings())
    assert exact.total_depth_within_window_usd == Decimal("2000")
    assert exact.depth_passed


def test_duplicate_levels_not_double_counted_and_unverified_volume_fails() -> None:
    book = snapshot(volume=None, source=None)
    book = book.model_copy(update={"bids": [book.bids[0], book.bids[0]]})
    depth = CombinedTwoSidedDepth().calculate(book, Decimal("0.52"), Decimal("0.03"))
    assert depth.bid_usd == Decimal("1000")
    assert "volume_unverified" in qualify_liquidity(book, Settings()).rejection_reasons


def test_calculated_rolling_volume_boundary() -> None:
    trades = [
        Trade(execution_price=Decimal("0.5"), executed_quantity=Decimal("10000"), executed_at=NOW),
        Trade(
            execution_price=Decimal("0.5"),
            executed_quantity=Decimal("9999"),
            executed_at=NOW - timedelta(hours=24),
        ),
        Trade(
            execution_price=Decimal("0.5"),
            executed_quantity=Decimal("9999"),
            executed_at=NOW - timedelta(hours=25),
        ),
    ]
    assert calculate_trailing_volume(trades, NOW) == Decimal("5000.0")
