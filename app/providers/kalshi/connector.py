from __future__ import annotations

import base64
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol, cast

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Provider
from app.providers.records import (
    ProviderBookLevel,
    ProviderEvent,
    ProviderHealthRecord,
    ProviderMarket,
    ProviderOrderBook,
    ProviderOutcome,
    ProviderTrade,
)


class RequestSigner(Protocol):
    def headers(self, method: str, path: str) -> dict[str, str]: ...


class KalshiRequestSigner:
    def __init__(self, key_id: str, private_key_path: str) -> None:
        key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi private key must be RSA")
        self._key_id = key_id
        self._key = key

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path.split('?')[0]}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


class KalshiEventPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str | None = None
    category: str | None = None
    strike_date: datetime | None = None
    status: str = "open"
    product_metadata: dict[str, Any] = Field(default_factory=dict)
    markets: list[dict[str, Any]] = Field(default_factory=list)


class KalshiMarketPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    ticker: str
    event_ticker: str
    title: str = ""
    subtitle: str = ""
    yes_sub_title: str = "Yes"
    no_sub_title: str = "No"
    status: str
    close_time: datetime | None = None
    occurrence_datetime: datetime | None = None
    primary_participant_key: str | None = None
    volume_24h_fp: Decimal | None = None


class KalshiMilestonePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    start_date: datetime
    primary_event_tickers: list[str] = Field(default_factory=list)
    related_event_tickers: list[str] = Field(default_factory=list)


class KalshiTradePayload(BaseModel):
    trade_id: str
    ticker: str
    count_fp: Decimal
    yes_price_dollars: Decimal
    created_time: datetime


class KalshiConnector:
    """Read-only Kalshi REST connector. It intentionally exposes no portfolio methods."""

    def __init__(
        self,
        base_url: str,
        signer: RequestSigner,
        client: httpx.AsyncClient | None = None,
        mode: str = "live",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._signer = signer
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=15)
        self._owns_client = client is None
        self._health = ProviderHealthRecord(
            provider=Provider.KALSHI, mode=mode, enabled=True, connected=False
        )
        self._soccer_game_series_cache: set[str] | None = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        started = perf_counter()
        attempted = datetime.now(UTC)
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._signer.headers("GET", f"/trade-api/v2{path}"),
            )
            response.raise_for_status()
            now = datetime.now(UTC)
            self._health = self._health.model_copy(
                update={
                    "connected": True,
                    "last_attempt_at": attempted,
                    "last_success_at": now,
                    "consecutive_failures": 0,
                    "latency_ms": Decimal(str((perf_counter() - started) * 1000)),
                }
            )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._health = self._health.model_copy(
                update={
                    "connected": False,
                    "last_attempt_at": attempted,
                    "last_failure_at": datetime.now(UTC),
                    "consecutive_failures": self._health.consecutive_failures + 1,
                    "latest_error_code": type(exc).__name__,
                    "sanitized_latest_error": str(exc)[:200],
                }
            )
            raise

    async def discover_events(
        self, start_time: datetime, end_time: datetime
    ) -> list[ProviderEvent]:
        cursor = ""
        records: list[ProviderEvent] = []
        soccer_game_series = await self._soccer_game_series()
        print(
            "//////////////////////// kalshi discover_events start_time:",
            start_time,
            "end_time:",
            end_time,
        )
        while True:
            payload = cast(
                dict[str, Any],
                await self._get(
                    "/events",
                    {
                        "status": "open",
                        "with_nested_markets": "true",
                        "with_milestones": "true",
                        "min_close_ts": int(start_time.timestamp()),
                        "limit": 200,
                        **({"cursor": cursor} if cursor else {}),
                    },
                ),
            )
            milestone_start_by_event: dict[str, datetime] = {}
            for raw_milestone in payload.get("milestones", []):
                milestone = KalshiMilestonePayload.model_validate(raw_milestone)
                for event_ticker in {
                    *milestone.primary_event_tickers,
                    *milestone.related_event_tickers,
                }:
                    existing = milestone_start_by_event.get(event_ticker)
                    if existing is None or milestone.start_date < existing:
                        milestone_start_by_event[event_ticker] = milestone.start_date
            # print(f"Discovered events: {len(payload.get('events', []))}")
            for raw in payload.get("events", []):
                event = KalshiEventPayload.model_validate(raw)
                if event.series_ticker not in soccer_game_series:
                    continue
                markets = [KalshiMarketPayload.model_validate(item) for item in event.markets]
                occurrence_times = [
                    market.occurrence_datetime
                    for market in markets
                    if market.occurrence_datetime is not None
                ]
                scheduled = milestone_start_by_event.get(event.event_ticker)
                if scheduled is None:
                    scheduled = min(occurrence_times) if occurrence_times else event.strike_date
                if scheduled is None or not start_time <= scheduled < end_time:
                    continue
                extracted = self.extract_participants(event, markets)
                if extracted is None:
                    continue
                participant_one, participant_two, source = extracted
                orientation_known = source in {
                    "event_title",
                    "event_sub_title",
                    "product_metadata:home_team,away_team",
                    "product_metadata:homeTeam,awayTeam",
                }
                competition = str(event.product_metadata.get("competition") or "").strip()
                records.append(
                    ProviderEvent(
                        provider=Provider.KALSHI,
                        provider_event_id=event.event_ticker,
                        title=event.title,
                        category=competition or event.category,
                        sport="soccer",
                        competition=competition or None,
                        home_team=participant_one if orientation_known else None,
                        away_team=participant_two if orientation_known else None,
                        participant_one=participant_one,
                        participant_two=participant_two,
                        orientation_known=orientation_known,
                        extraction_source=source,
                        scheduled_start=scheduled,
                        status=event.status,
                        raw_market_ids=[market.ticker for market in markets],
                    )
                )
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        print(f"Total events from kalshi discovered: {len(records)}")
        # print(records)
        return records

    async def _soccer_game_series(self) -> set[str]:
        if self._soccer_game_series_cache is not None:
            return self._soccer_game_series_cache
        payload = cast(
            dict[str, Any],
            await self._get(
                "/series",
                {
                    "category": "Sports",
                    "tags": "Soccer",
                    "include_product_metadata": "true",
                },
            ),
        )
        self._soccer_game_series_cache = {
            str(item["ticker"])
            for item in payload.get("series", [])
            if str(item.get("category", "")).casefold() == "sports"
            and "soccer" in {str(tag).casefold() for tag in item.get("tags", [])}
            and str((item.get("product_metadata") or {}).get("scope", "")).casefold() == "game"
        }
        return self._soccer_game_series_cache

    @staticmethod
    def extract_participants(
        event: KalshiEventPayload, markets: list[KalshiMarketPayload]
    ) -> tuple[str, str, str] | None:
        """Extract an unordered team pair without parsing Kalshi ticker conventions."""

        metadata = event.product_metadata
        for first_key, second_key in (
            ("home_team", "away_team"),
            ("homeTeam", "awayTeam"),
            ("participant_1", "participant_2"),
            ("participant1", "participant2"),
        ):
            first = str(metadata.get(first_key) or "").strip()
            second = str(metadata.get(second_key) or "").strip()
            if first and second and first.casefold() != second.casefold():
                return first, second, f"product_metadata:{first_key},{second_key}"

        for text, source in (
            (event.title, "event_title"),
            (event.sub_title or "", "event_sub_title"),
        ):
            parts = re.split(r"\s+(?:vs?\.?|at|@)\s+", text, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                first = parts[0].strip(" -:()")
                second = KalshiConnector._strip_market_descriptor(parts[1])
                if first and second and first.casefold() != second.casefold():
                    return first, second, source

        excluded = {"yes", "no", "draw", "tie"}
        outcomes = list(
            dict.fromkeys(
                market.yes_sub_title.strip()
                for market in markets
                if market.yes_sub_title.strip().casefold() not in excluded
            )
        )
        if len(outcomes) == 2:
            return outcomes[0], outcomes[1], "market_yes_sub_titles"
        return None

    @staticmethod
    def _strip_market_descriptor(value: str) -> str:
        value = re.sub(r"\s+\([^)]*\)$", "", value).strip(" -:()")
        matchup, separator, descriptor = value.partition(":")
        if separator and re.search(
            r"\b(regulation\s+time|90\s*minutes?|moneyline|match\s+winner)\b",
            descriptor,
            flags=re.IGNORECASE,
        ):
            return matchup.strip(" -:()")
        return value

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        payload = cast(
            dict[str, Any],
            await self._get(
                "/markets",
                {"event_ticker": event_id, "status": "open", "limit": 1000},
            ),
        )
        records = [self.parse_market(item) for item in payload.get("markets", [])]
        self._health = self._health.model_copy(update={"markets_discovered": len(records)})
        return records

    @staticmethod
    def parse_market(raw: object) -> ProviderMarket:
        market = KalshiMarketPayload.model_validate(raw)
        return ProviderMarket(
            provider=Provider.KALSHI,
            provider_event_id=market.event_ticker,
            provider_market_id=market.ticker,
            title=market.title or market.subtitle,
            status=market.status,
            order_book_enabled=True,
            outcomes=[
                ProviderOutcome(name=market.yes_sub_title, selection_id="yes"),
                ProviderOutcome(name=market.no_sub_title, selection_id="no"),
            ],
            close_time=market.close_time,
            # Kalshi reports contracts; reliable USD volume is calculated from trades.
            trailing_24h_volume_usd=None,
        )

    async def get_order_book(self, market_or_token_id: str) -> ProviderOrderBook:
        payload = cast(
            dict[str, Any],
            await self._get(f"/markets/{market_or_token_id}/orderbook", {"depth": 100}),
        )
        book = cast(dict[str, Any], payload.get("orderbook_fp") or {})
        yes = [
            ProviderBookLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in book.get("yes_dollars", [])
        ]
        no = [
            ProviderBookLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in book.get("no_dollars", [])
        ]
        asks = [
            ProviderBookLevel(price=Decimal("1") - level.price, quantity=level.quantity)
            for level in no
        ]
        now = datetime.now(UTC)
        result = ProviderOrderBook(
            provider=Provider.KALSHI,
            provider_market_id=market_or_token_id,
            selection_id="yes",
            bids=sorted(yes, key=lambda level: level.price, reverse=True),
            asks=sorted(asks, key=lambda level: level.price),
            source_timestamp=now,
            ask_derived=bool(asks),
            source_no_price=max((level.price for level in no), default=None),
        )
        self._health = self._health.model_copy(
            update={
                "books_updated": self._health.books_updated + 1,
                "last_order_book_timestamp": now,
            }
        )
        return result

    async def get_recent_trades(
        self, market_or_token_id: str, since: datetime
    ) -> list[ProviderTrade]:
        cursor = ""
        records: list[ProviderTrade] = []
        while True:
            payload = cast(
                dict[str, Any],
                await self._get(
                    "/markets/trades",
                    {
                        "ticker": market_or_token_id,
                        "min_ts": int(since.timestamp()),
                        "limit": 1000,
                        **({"cursor": cursor} if cursor else {}),
                    },
                ),
            )
            records.extend(
                ProviderTrade(
                    provider=Provider.KALSHI,
                    provider_market_id=trade.ticker,
                    trade_id=trade.trade_id,
                    price=trade.yes_price_dollars,
                    quantity=trade.count_fp,
                    executed_at=trade.created_time,
                )
                for trade in (
                    KalshiTradePayload.model_validate(item) for item in payload.get("trades", [])
                )
            )
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        self._health = self._health.model_copy(update={"trades_processed": len(records)})
        return records

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
