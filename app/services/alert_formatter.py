from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.domain.models import Opportunity, PredictionMarketQuote, SportsbookQuote


def _newest_unique_predictions(
    quotes: Sequence[PredictionMarketQuote],
) -> list[PredictionMarketQuote]:
    unique: dict[tuple[object, ...], PredictionMarketQuote] = {}
    for quote in quotes:
        key = (
            quote.provider,
            quote.provider_market_id,
            quote.market_type,
            quote.selection,
            quote.line,
            quote.participant,
        )
        current = unique.get(key)
        if current is None or quote.received_timestamp > current.received_timestamp:
            unique[key] = quote
    return list(unique.values())


def _newest_unique_sportsbooks(quotes: Sequence[SportsbookQuote]) -> list[SportsbookQuote]:
    unique: dict[tuple[object, ...], SportsbookQuote] = {}
    for quote in quotes:
        key = (
            quote.bookmaker_id,
            quote.market_type,
            quote.selection,
            quote.line,
            quote.participant,
        )
        current = unique.get(key)
        if current is None or quote.received_timestamp > current.received_timestamp:
            unique[key] = quote
    return list(unique.values())


def format_telegram_alert(
    best: Opportunity,
    predictions: Sequence[PredictionMarketQuote],
    sportsbooks: Sequence[SportsbookQuote],
    client_timezone: str = "America/Los_Angeles",
    depth_window_from_midpoint_cents: Decimal = Decimal("3"),
) -> str:
    localized_kickoff = best.kickoff_time_utc.astimezone(ZoneInfo(client_timezone))
    depth_window_label = f"{depth_window_from_midpoint_cents:g}"
    depth_window_unit = "cent" if depth_window_from_midpoint_cents == Decimal("1") else "cents"
    matching_predictions = _newest_unique_predictions(
        [
            quote
            for quote in predictions
            if quote.canonical_event_id == best.canonical_event_id
            and quote.market_type == best.market_type
            and quote.selection == best.selection
            and quote.line == best.line
            and quote.participant == best.participant
        ]
    )
    matching_sportsbooks = _newest_unique_sportsbooks(
        [
            quote
            for quote in sportsbooks
            if quote.canonical_event_id == best.canonical_event_id
            and quote.market_type == best.market_type
            and quote.selection == best.selection
            and quote.line == best.line
            and quote.participant == best.participant
        ]
    )
    selection = best.selection.value.title()
    if best.participant:
        selection = best.participant
    if best.line is not None:
        selection = (
            f"{selection} {best.line:+}"
            if best.market_type == "spread"
            else (f"{selection} {best.line}")
        )
    prediction_lines = "\n\n".join(
        f"{q.provider.value.title()}\n"
        f"Bid: {q.best_bid_probability:.1%} × {q.best_bid_size:,.0f}\n"
        f"Ask: {q.best_ask_probability:.1%} × {q.best_ask_size:,.0f}"
        + (
            " — used for edge"
            if q.provider == best.prediction_market_provider
            and q.provider_market_id == best.prediction_market_id
            else ""
        )
        for q in matching_predictions
    )
    largest_sportsbook = min(
        matching_sportsbooks,
        key=lambda quote: quote.implied_probability,
        default=None,
    )
    sportsbook_lines = "\n".join(
        f"{q.bookmaker_display_name}: {q.decimal_odds:.2f} → {q.implied_probability:.2%}"
        + (
            " — "
            + ", ".join(
                label
                for label, applies in (
                    ("this alert pair", q.bookmaker_id == best.bookmaker_id),
                    (
                        "largest edge",
                        largest_sportsbook is not None
                        and q.bookmaker_id == largest_sportsbook.bookmaker_id,
                    ),
                )
                if applies
            )
            if q.bookmaker_id == best.bookmaker_id
            or (
                largest_sportsbook is not None
                and q.bookmaker_id == largest_sportsbook.bookmaker_id
            )
            else ""
        )
        for q in matching_sportsbooks
    )
    largest_edge_line = ""
    if largest_sportsbook is not None:
        largest_edge = (
            best.prediction_market_best_ask - largest_sportsbook.implied_probability
        ) * 100
        largest_edge_line = (
            "Largest sportsbook edge for this prediction ask: "
            f"{largest_sportsbook.bookmaker_display_name} "
            f"({largest_edge:+.2f} percentage points)\n"
        )
    links = "\n".join(
        link for link in [best.prediction_market_direct_url, best.sportsbook_direct_url] if link
    )
    period_label = (
        "90 Minutes"
        if best.sport == "soccer" and best.period.value == "regulation"
        else best.period.value.replace("_", " ").title()
    )
    return (
        f"🚨 {best.sport.upper()} PRICE DISCREPANCY\n\n"
        f"{best.competition}\n{best.home_team} vs {best.away_team}\n"
        f"Kickoff: {localized_kickoff:%Y-%m-%d %I:%M %p %Z}\n\n"
        f"Market:\n{best.market_type.value.title()} — {selection} — {period_label}\n\n"
        "QUALIFYING PAIR\n\n"
        f"Value side: Sportsbook — {best.bookmaker_display_name}\n"
        "Reference probability: Prediction market — "
        f"{best.prediction_market_provider.value.title()} "
        f"({best.prediction_market_best_ask:.1%})\n"
        "Interpretation: The sportsbook offers the better price; this is not a signal "
        "to buy the prediction-market YES contract.\n\n"
        f"Prediction market: {best.prediction_market_provider.value.title()}\n"
        f"Executable YES ask: {best.prediction_market_best_ask:.1%}\n"
        f"Ask liquidity: {best.prediction_market_ask_size:,.0f} contracts\n\n"
        f"Sportsbook: {best.bookmaker_display_name}\n"
        f"Odds: {best.sportsbook_decimal_odds:.2f}\n"
        f"Implied probability: {best.sportsbook_implied_probability:.2%}\n\n"
        f"Edge: +{best.edge_percentage_points:.2f} percentage points\n"
        f"Threshold: {best.configured_threshold:.2f}% PASSED\n"
        f"{largest_edge_line}\n"
        "MARKET QUALITY\n\n"
        f"Spread: {best.spread_cents:.1f} cents — PASSED\n"
        f"Depth within {depth_window_label} {depth_window_unit} of midpoint: "
        f"${best.total_depth_within_window_usd:,.0f} — PASSED\n"
        f"Trailing 24-hour volume: ${best.trailing_24h_volume_usd:,.0f} — PASSED\n"
        "Pregame timing: PASSED\n\n"
        f"PREDICTION MARKETS\n\n{prediction_lines}\n\n"
        f"SPORTSBOOKS\n\n{sportsbook_lines}\n\n"
        f"QUALITY\n\n✓ Exact/approved event match\n✓ Same {period_label} settlement\n"
        "✓ Prices fresh\n✓ Liquidity requirement passed\n\n"
        "Updated: "
        f"{max(best.prediction_quote_age_seconds, best.sportsbook_quote_age_seconds):.0f}s ago"
        + (f"\n\n{links}" if links else "")
    )


class TelegramSender(Protocol):
    async def send(self, message: str) -> None: ...


class MockTelegramSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class FanoutTelegramSender:
    """Attempt delivery to every configured Telegram destination."""

    def __init__(self, senders: Sequence[TelegramSender]) -> None:
        self._senders = list(senders)

    async def send(self, message: str) -> None:
        failures: list[Exception] = []
        for sender in self._senders:
            try:
                await sender.send(message)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError(
                f"Telegram delivery failed for {len(failures)} of {len(self._senders)} destinations"
            ) from failures[0]


class TelegramHttpSender:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None

    async def send(self, message: str) -> None:
        response = await self._client.post(
            self._url,
            json={"chat_id": self._chat_id, "text": message, "disable_web_page_preview": True},
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
