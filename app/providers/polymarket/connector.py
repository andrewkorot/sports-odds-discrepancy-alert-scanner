from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("expected JSON array")
        return [str(item) for item in parsed]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("expected array")


class GammaMarketPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    conditionId: str
    question: str
    active: bool = True
    closed: bool = False
    enableOrderBook: bool = False
    clobTokenIds: list[str]
    outcomes: list[str]
    endDate: datetime | None = None
    eventStartTime: datetime | None = None
    sportsMarketType: str | None = None
    volume24hr: Decimal | None = None

    @field_validator("clobTokenIds", "outcomes", mode="before")
    @classmethod
    def parse_arrays(cls, value: object) -> list[str]:
        return _json_list(value)


class GammaEventPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    active: bool = True
    closed: bool = False
    startDate: datetime | None = None
    endDate: datetime | None = None
    startTime: datetime | None = None
    live: bool = False
    ended: bool = False
    series: list[dict[str, Any]] = Field(default_factory=list)
    teams: list[dict[str, Any]] = Field(default_factory=list)
    markets: list[GammaMarketPayload] = Field(default_factory=list)
    tags: list[dict[str, Any]] = Field(default_factory=list)


class DataTradePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conditionId: str
    asset: str
    size: Decimal
    price: Decimal
    timestamp: int
    transactionHash: str


class PolymarketConnector:
    """Public, read-only Gamma/CLOB/Data connector with no wallet or API credentials."""

    def __init__(
        self,
        gamma_base_url: str,
        clob_base_url: str,
        data_base_url: str,
        client: httpx.AsyncClient | None = None,
        mode: str = "live",
    ) -> None:
        self._gamma = gamma_base_url.rstrip("/")
        self._clob = clob_base_url.rstrip("/")
        self._data = data_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None
        self._health = ProviderHealthRecord(
            provider=Provider.POLYMARKET, mode=mode, enabled=True, connected=False
        )

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        attempted = datetime.now(UTC)
        started = perf_counter()
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            self._health = self._health.model_copy(
                update={
                    "connected": True,
                    "last_attempt_at": attempted,
                    "last_success_at": datetime.now(UTC),
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
        seen_cursors: set[str] = set()
        records: list[ProviderEvent] = []
        print(
            "//////////////////////// polymarket discover_events start_time:",
            start_time,
            "end_time:",
            end_time,
        )
        while True:
            payload = cast(
                dict[str, Any],
                await self._get(
                    f"{self._gamma}/events/keyset",
                    {
                        "active": "true",
                        "closed": "false",
                        "tag_slug": "soccer",
                        "start_time_min": start_time.isoformat(),
                        "start_time_max": end_time.isoformat(),
                        "limit": 500,
                        **({"after_cursor": cursor} if cursor else {}),
                    },
                ),
            )
            for raw in payload.get("events", []):
                event = GammaEventPayload.model_validate(raw)
                if event.live or event.ended:
                    continue
                eligible_markets = [
                    market
                    for market in event.markets
                    if market.active
                    and not market.closed
                    and market.enableOrderBook
                    and (market.sportsMarketType or "").casefold() == "moneyline"
                ]
                if not eligible_markets:
                    continue
                tag_labels = [
                    str(tag.get("label", "")).strip()
                    for tag in event.tags
                    if str(tag.get("label", "")).strip()
                ]
                scheduled = self.fixture_start(event, eligible_markets)
                if scheduled is None or not start_time <= scheduled <= end_time:
                    continue
                home_team, away_team = self.ordered_teams(event)
                if home_team is None or away_team is None:
                    continue
                competition = self.competition_name(event, tag_labels)
                records.append(
                    ProviderEvent(
                        provider=Provider.POLYMARKET,
                        provider_event_id=event.id,
                        title=event.title,
                        category=competition,
                        sport="soccer",
                        competition=competition,
                        home_team=home_team,
                        away_team=away_team,
                        scheduled_start=scheduled,
                        status="open" if event.active and not event.closed else "closed",
                        raw_market_ids=[market.id for market in eligible_markets],
                    )
                )
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        self._health = self._health.model_copy(update={"events_discovered": len(records)})
        print(f"Total events discovered: {len(records)}")
        # print(records)
        return records

    @staticmethod
    def fixture_start(
        event: GammaEventPayload, eligible_markets: list[GammaMarketPayload]
    ) -> datetime | None:
        """Return fixture kickoff without treating Gamma's creation startDate as kickoff."""

        if event.startTime is not None:
            return event.startTime
        market_start_times = [
            market.eventStartTime
            for market in eligible_markets
            if market.eventStartTime is not None
        ]
        if market_start_times:
            return min(market_start_times)
        if event.endDate is not None:
            return event.endDate
        market_end_times = [
            market.endDate for market in eligible_markets if market.endDate is not None
        ]
        return min(market_end_times) if market_end_times else None

    @staticmethod
    def ordered_teams(event: GammaEventPayload) -> tuple[str | None, str | None]:
        ordered = {
            str(team.get("ordering", "")).casefold(): str(team.get("name", "")).strip()
            for team in event.teams
            if str(team.get("name", "")).strip()
        }
        home = ordered.get("home")
        away = ordered.get("away")
        if home and away:
            return home, away
        title_parts = [
            part.strip(" -:")
            for part in re.split(r"\s+vs?\.?\s+", event.title, maxsplit=1, flags=re.IGNORECASE)
        ]
        if len(title_parts) == 2 and all(title_parts):
            return title_parts[0], title_parts[1]
        return None, None

    @staticmethod
    def competition_name(event: GammaEventPayload, tag_labels: list[str]) -> str:
        for series in event.series:
            title = str(series.get("title", "")).strip()
            if title:
                return title
        return next(
            (
                label
                for label in tag_labels
                if label.casefold() not in {"soccer", "sports", "games"}
            ),
            "Soccer",
        )

    async def discover_markets(self, event_id: str) -> list[ProviderMarket]:
        raw = cast(dict[str, Any], await self._get(f"{self._gamma}/events/{event_id}"))
        event = GammaEventPayload.model_validate(raw)
        records = [
            self.parse_market(event.id, market)
            for market in event.markets
            if (market.sportsMarketType or "").casefold() == "moneyline"
        ]
        self._health = self._health.model_copy(update={"markets_discovered": len(records)})
        return records

    @staticmethod
    def parse_market(event_id: str, raw: GammaMarketPayload | dict[str, Any]) -> ProviderMarket:
        market = (
            raw if isinstance(raw, GammaMarketPayload) else GammaMarketPayload.model_validate(raw)
        )
        if len(market.outcomes) != len(market.clobTokenIds):
            raise ValueError("Polymarket outcome/token relationship is incomplete")
        return ProviderMarket(
            provider=Provider.POLYMARKET,
            provider_event_id=event_id,
            provider_market_id=market.id,
            condition_id=market.conditionId,
            title=market.question,
            status="open" if market.active and not market.closed else "closed",
            order_book_enabled=market.enableOrderBook,
            outcomes=[
                ProviderOutcome(name=name, selection_id=str(index), token_id=token)
                for index, (name, token) in enumerate(
                    zip(market.outcomes, market.clobTokenIds, strict=True)
                )
            ],
            close_time=market.endDate,
            # Gamma's documented volume24hr is mapped explicitly when present.
            trailing_24h_volume_usd=market.volume24hr,
        )

    async def get_order_book(self, market_or_token_id: str) -> ProviderOrderBook:
        payload = cast(
            dict[str, Any],
            await self._get(f"{self._clob}/book", {"token_id": market_or_token_id}),
        )
        timestamp_raw = payload.get("timestamp")
        if timestamp_raw is None:
            raise ValueError("Polymarket order book timestamp is missing")
        timestamp_text = str(timestamp_raw)
        timestamp = (
            datetime.fromtimestamp(int(timestamp_text) / 1000, UTC)
            if timestamp_text.isdigit()
            else datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        )
        result = ProviderOrderBook(
            provider=Provider.POLYMARKET,
            provider_market_id=str(payload.get("market") or ""),
            selection_id=str(payload.get("asset_id") or market_or_token_id),
            bids=sorted(
                (
                    ProviderBookLevel(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["size"])),
                    )
                    for item in payload.get("bids", [])
                ),
                key=lambda level: level.price,
                reverse=True,
            ),
            asks=sorted(
                (
                    ProviderBookLevel(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["size"])),
                    )
                    for item in payload.get("asks", [])
                ),
                key=lambda level: level.price,
            ),
            source_timestamp=timestamp,
        )
        self._health = self._health.model_copy(
            update={
                "books_updated": self._health.books_updated + 1,
                "last_order_book_timestamp": timestamp,
            }
        )
        return result

    async def get_recent_trades(
        self, market_or_token_id: str, since: datetime
    ) -> list[ProviderTrade]:
        offset = 0
        records: list[ProviderTrade] = []
        while True:
            page = cast(
                list[dict[str, Any]],
                await self._get(
                    f"{self._data}/trades",
                    {"market": market_or_token_id, "limit": 1000, "offset": offset},
                ),
            )
            parsed = [DataTradePayload.model_validate(item) for item in page]
            records.extend(
                ProviderTrade(
                    provider=Provider.POLYMARKET,
                    provider_market_id=trade.conditionId,
                    trade_id=trade.transactionHash,
                    price=trade.price,
                    quantity=trade.size,
                    executed_at=datetime.fromtimestamp(trade.timestamp, UTC),
                )
                for trade in parsed
                if datetime.fromtimestamp(trade.timestamp, UTC) > since
            )
            if len(page) < 1000:
                break
            offset += len(page)
        self._health = self._health.model_copy(update={"trades_processed": len(records)})
        return records

    async def health(self) -> ProviderHealthRecord:
        return self._health

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
