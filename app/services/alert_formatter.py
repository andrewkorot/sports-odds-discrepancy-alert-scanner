from collections.abc import Sequence
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.domain.models import Opportunity, PredictionMarketQuote, SportsbookQuote


def format_telegram_alert(
    best: Opportunity,
    predictions: Sequence[PredictionMarketQuote],
    sportsbooks: Sequence[SportsbookQuote],
    client_timezone: str = "America/Los_Angeles",
) -> str:
    localized_kickoff = best.kickoff_time_utc.astimezone(ZoneInfo(client_timezone))
    matching_predictions = [
        quote
        for quote in predictions
        if quote.canonical_event_id == best.canonical_event_id
        and quote.market_type == best.market_type
        and quote.selection == best.selection
        and quote.line == best.line
        and quote.participant == best.participant
    ]
    matching_sportsbooks = [
        quote
        for quote in sportsbooks
        if quote.canonical_event_id == best.canonical_event_id
        and quote.market_type == best.market_type
        and quote.selection == best.selection
        and quote.line == best.line
        and quote.participant == best.participant
    ]
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
        + (" — used for edge" if q.provider == best.prediction_market_provider else "")
        for q in matching_predictions
    )
    sportsbook_lines = "\n".join(
        f"{q.bookmaker_display_name}: {q.decimal_odds:.2f} → {q.implied_probability:.2%}"
        for q in matching_sportsbooks
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
        "BEST OPPORTUNITY\n\n"
        f"Prediction market: {best.prediction_market_provider.value.title()}\n"
        f"Executable YES ask: {best.prediction_market_best_ask:.1%}\n"
        f"Ask liquidity: {best.prediction_market_ask_size:,.0f} contracts\n\n"
        f"Sportsbook: {best.bookmaker_display_name}\n"
        f"Odds: {best.sportsbook_decimal_odds:.2f}\n"
        f"Implied probability: {best.sportsbook_implied_probability:.2%}\n\n"
        f"Edge: +{best.edge_percentage_points:.2f} percentage points\n"
        f"Threshold: {best.configured_threshold:.2f}% PASSED\n\n"
        "MARKET QUALITY\n\n"
        f"Spread: {best.spread_cents:.1f} cents — PASSED\n"
        f"Depth within 3 cents of midpoint: "
        f"${best.total_depth_within_window_usd:,.0f} — PASSED\n"
        f"Trailing 24-hour volume: ${best.trailing_24h_volume_usd:,.0f} — PASSED\n"
        "Game date: Today — PASSED\n\n"
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
