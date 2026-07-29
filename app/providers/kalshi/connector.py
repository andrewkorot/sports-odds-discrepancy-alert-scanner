from __future__ import annotations

import base64
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
    title: str
    category: str | None = None
    strike_date: datetime | None = None
    status: str = "open"
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
    volume_24h_fp: Decimal | None = None


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
        while True:
            payload = cast(
                dict[str, Any],
                await self._get(
                    "/events",
                    {
                        "status": "open",
                        "with_nested_markets": "true",
                        "min_close_ts": int(start_time.timestamp()),
                        "limit": 200,
                        **({"cursor": cursor} if cursor else {}),
                    },
                ),
            )
            for raw in payload.get("events", []):
                event = KalshiEventPayload.model_validate(raw)
                scheduled = event.strike_date
                if scheduled and not start_time <= scheduled <= end_time:
                    continue
                searchable = f"{event.category or ''} {event.title}".casefold()
                if not any(term in searchable for term in ("soccer", "mls", "premier league")):
                    continue
                records.append(
                    ProviderEvent(
                        provider=Provider.KALSHI,
                        provider_event_id=event.event_ticker,
                        title=event.title,
                        category=event.category,
                        scheduled_start=scheduled,
                        status=event.status,
                        raw_market_ids=[str(item.get("ticker")) for item in event.markets],
                    )
                )
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        return records

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
