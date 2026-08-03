import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.domain.enums import AvailabilityStatus, Provider, Selection, VolumeSource
from app.domain.models import Bookmaker
from app.providers.kalshi.connector import (
    KalshiConnector,
    KalshiEventPayload,
    KalshiMarketPayload,
)
from app.providers.oddspapi.connector import (
    OddsPapiHTTPError,
    SportsOddsConnector,
    bookmaker_outcome_agrees,
    sanitized_error,
)
from app.providers.polymarket.connector import GammaEventPayload, PolymarketConnector
from app.providers.records import ProviderEvent
from app.services.provider_normalization import normalize_order_book

FIXTURES = Path(__file__).parents[1] / "fixtures"


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "sanitized"}


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
async def test_kalshi_orderbook_derives_yes_ask_and_preserves_no_quantity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/KXMLS/orderbook")
        return httpx.Response(200, json=fixture("kalshi_orderbook.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = KalshiConnector(
        "https://external-api.kalshi.com/trade-api/v2", FakeSigner(), client
    )
    book = await connector.get_order_book("KXMLS")
    assert book.bids[0].price == Decimal("0.51")
    assert book.asks[0].price == Decimal("0.52")
    assert book.asks[0].quantity == Decimal("850")
    assert book.source_no_price == Decimal("0.48")
    assert book.ask_derived
    await client.aclose()


@pytest.mark.asyncio
async def test_kalshi_discovers_only_soccer_game_series_and_extracts_participants() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/series"):
            assert request.url.params["category"] == "Sports"
            assert request.url.params["tags"] == "Soccer"
            return httpx.Response(
                200,
                json={
                    "series": [
                        {
                            "ticker": "KXMLSGAME",
                            "category": "Sports",
                            "tags": ["Soccer"],
                            "product_metadata": {"scope": "Game"},
                        },
                        {
                            "ticker": "KXMLSCUP",
                            "category": "Sports",
                            "tags": ["Soccer"],
                            "product_metadata": {"scope": "Future"},
                        },
                    ]
                },
            )
        assert request.url.path.endswith("/events")
        assert request.url.params["with_milestones"] == "true"
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "event_ticker": "KXMLSGAME-26JUL30ATLIM",
                        "series_ticker": "KXMLSGAME",
                        "title": "Atlanta United vs Inter Miami CF",
                        "category": "Sports",
                        "product_metadata": {
                            "competition": "MLS",
                            "competition_scope": "Game",
                        },
                        "markets": [
                            {
                                "ticker": "KXMLSGAME-HOME",
                                "event_ticker": "KXMLSGAME-26JUL30ATLIM",
                                "title": "Will Inter Miami CF win?",
                                "yes_sub_title": "Inter Miami CF",
                                "no_sub_title": "Inter Miami CF",
                                "status": "open",
                                "occurrence_datetime": "2026-07-30T20:00:00Z",
                            },
                            {
                                "ticker": "KXMLSGAME-AWAY",
                                "event_ticker": "KXMLSGAME-26JUL30ATLIM",
                                "title": "Will Atlanta United win?",
                                "yes_sub_title": "Atlanta United",
                                "no_sub_title": "Atlanta United",
                                "status": "open",
                                "occurrence_datetime": "2026-07-30T20:00:00Z",
                            },
                        ],
                    },
                    {
                        "event_ticker": "KXMLSCUP-26",
                        "series_ticker": "KXMLSCUP",
                        "title": "MLS Cup Champion",
                        "category": "Sports",
                        "markets": [],
                    },
                ],
                "milestones": [
                    {
                        "id": "mls-game",
                        "category": "Sports",
                        "type": "soccer_game",
                        "title": "Atlanta United vs Inter Miami CF",
                        "start_date": "2026-07-30T17:00:00Z",
                        "primary_event_tickers": ["KXMLSGAME-26JUL30ATLIM"],
                        "related_event_tickers": ["KXMLSGAME-26JUL30ATLIM"],
                    }
                ],
                "cursor": "",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = KalshiConnector(
        "https://external-api.kalshi.com/trade-api/v2", FakeSigner(), client
    )
    start = datetime(2026, 7, 30, 16, tzinfo=UTC)
    events = await connector.discover_events(start, start + timedelta(hours=72))

    assert len(events) == 1
    assert events[0].participant_one == "Atlanta United"
    assert events[0].participant_two == "Inter Miami CF"
    assert events[0].orientation_known
    assert events[0].extraction_source == "event_title"
    assert events[0].home_team == "Atlanta United"
    assert events[0].away_team == "Inter Miami CF"
    assert events[0].competition == "MLS"
    assert events[0].scheduled_start == datetime(2026, 7, 30, 17, tzinfo=UTC)
    await client.aclose()


def test_kalshi_participants_fall_back_to_two_market_outcomes() -> None:
    event = KalshiEventPayload(
        event_ticker="event",
        series_ticker="series",
        title="Match winner",
    )
    markets = [
        KalshiMarketPayload(
            ticker=f"market-{team}",
            event_ticker="event",
            title=f"Will {team} win?",
            yes_sub_title=team,
            no_sub_title=team,
            status="open",
        )
        for team in ("Inter Miami", "Atlanta United")
    ]

    extracted = KalshiConnector.extract_participants(event, markets)

    assert extracted == (
        "Inter Miami",
        "Atlanta United",
        "market_yes_sub_titles",
    )


def test_kalshi_title_removes_regulation_moneyline_descriptor() -> None:
    event = KalshiEventPayload(
        event_ticker="event",
        series_ticker="series",
        title="Panathinaikos vs Paksi: Regulation Time Moneyline",
    )

    extracted = KalshiConnector.extract_participants(event, [])

    assert extracted == ("Panathinaikos", "Paksi", "event_title")


def test_polymarket_market_preserves_outcome_token_relationship() -> None:
    raw = fixture("polymarket_market.json")
    assert isinstance(raw, dict)
    market = PolymarketConnector.parse_market("event-1", raw)
    assert market.condition_id == "0xcondition"
    assert [(item.name, item.token_id) for item in market.outcomes] == [
        ("Yes", "yes-token"),
        ("No", "no-token"),
    ]
    assert market.trailing_24h_volume_usd == Decimal("8200.50")


def test_polymarket_rejects_mismatched_outcome_tokens() -> None:
    raw = fixture("polymarket_market.json")
    assert isinstance(raw, dict)
    raw["clobTokenIds"] = '["yes-token"]'
    with pytest.raises(ValueError, match="relationship"):
        PolymarketConnector.parse_market("event-1", raw)


@pytest.mark.asyncio
async def test_polymarket_public_orderbook_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "POLY_API_KEY" not in request.headers
        return httpx.Response(200, json=fixture("polymarket_orderbook.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = PolymarketConnector(
        "https://gamma-api.polymarket.com",
        "https://clob.polymarket.com",
        "https://data-api.polymarket.com",
        client,
    )
    book = await connector.get_order_book("yes-token")
    assert book.selection_id == "yes-token"
    assert book.bids[0].quantity == Decimal("4100")
    assert book.asks[0].price == Decimal("0.54")
    normalized = normalize_order_book(
        book,
        Selection.YES,
        book.source_timestamp,
        Decimal("8200"),
        VolumeSource.PROVIDER_REPORTED,
    )
    assert normalized.best_ask == Decimal("0.54")
    assert normalized.asks[0].notional_usd == Decimal("1836.00")
    duplicate_book = book.model_copy(update={"asks": [book.asks[0], book.asks[0]]})
    consolidated = normalize_order_book(
        duplicate_book,
        Selection.YES,
        book.source_timestamp,
    )
    assert len(consolidated.asks) == 1
    assert consolidated.asks[0].quantity == book.asks[0].quantity * 2
    await client.aclose()


def test_malformed_provider_payload_is_rejected() -> None:
    with pytest.raises((ValidationError, ValueError)):
        PolymarketConnector.parse_market("event-1", {"id": "incomplete"})


@pytest.mark.asyncio
async def test_polymarket_discovers_soccer_by_fixture_start_and_moneyline() -> None:
    requests: list[httpx.Request] = []

    def market(
        market_id: str, market_type: str, event_start: str | None = None
    ) -> dict[str, object]:
        return {
            "id": market_id,
            "conditionId": f"condition-{market_id}",
            "question": "Will Inter Miami win?",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "clobTokenIds": '["yes-token", "no-token"]',
            "outcomes": '["Yes", "No"]',
            "sportsMarketType": market_type,
            "eventStartTime": event_start,
            "endDate": "2026-07-30T20:00:00Z",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/events/keyset"
        assert request.url.params["tag_slug"] == "soccer"
        assert request.url.params["start_time_min"]
        assert request.url.params["start_time_max"]
        assert "start_date_min" not in request.url.params
        assert request.url.params["limit"] == "500"
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "match-1",
                        "title": "Inter Miami CF vs. Atlanta United FC",
                        # Creation date must never be treated as fixture kickoff.
                        "startDate": "2026-01-01T00:00:00Z",
                        "endDate": "2026-01-02T00:00:00Z",
                        "startTime": "2026-07-30T20:00:00Z",
                        "active": True,
                        "closed": False,
                        "teams": [
                            {"name": "Inter Miami CF", "ordering": "home"},
                            {"name": "Atlanta United FC", "ordering": "away"},
                        ],
                        "series": [{"title": "MLS", "slug": "mls"}],
                        "tags": [{"label": "Sports"}, {"label": "Soccer"}],
                        "markets": [
                            market("moneyline-1", "moneyline"),
                            market("halftime-1", "soccer_halftime_result"),
                        ],
                    },
                    {
                        "id": "award-1",
                        "title": "Ballon d'Or Winner",
                        "startTime": "2026-07-30T21:00:00Z",
                        "active": True,
                        "closed": False,
                        "markets": [market("award-market", "moneyline")],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = PolymarketConnector(
        "https://gamma-api.polymarket.com",
        "https://clob.polymarket.com",
        "https://data-api.polymarket.com",
        client,
    )
    start = datetime(2026, 7, 30, 16, tzinfo=UTC)
    events = await connector.discover_events(start, start + timedelta(hours=72))

    assert len(requests) == 1
    assert len(events) == 1
    assert events[0].scheduled_start == datetime(2026, 7, 30, 20, tzinfo=UTC)
    assert events[0].home_team == "Inter Miami CF"
    assert events[0].away_team == "Atlanta United FC"
    assert events[0].competition == "MLS"
    assert events[0].raw_market_ids == ["moneyline-1"]
    await client.aclose()


def test_polymarket_fixture_start_falls_back_without_using_creation_date() -> None:
    event = GammaEventPayload.model_validate(
        {
            "id": "event-1",
            "title": "Inter Miami vs Atlanta United",
            "startDate": "2026-01-01T00:00:00Z",
            "active": True,
            "closed": False,
            "markets": [
                {
                    "id": "market-1",
                    "conditionId": "condition-1",
                    "question": "Will Inter Miami win?",
                    "enableOrderBook": True,
                    "clobTokenIds": '["yes", "no"]',
                    "outcomes": '["Yes", "No"]',
                    "sportsMarketType": "moneyline",
                    "eventStartTime": "2026-07-30T20:00:00Z",
                }
            ],
        }
    )

    scheduled = PolymarketConnector.fixture_start(event, event.markets)

    assert scheduled == datetime(2026, 7, 30, 20, tzinfo=UTC)


def test_oddspapi_error_sanitization_does_not_include_request_url() -> None:
    request = httpx.Request("GET", "https://api.oddspapi.io/v4/bookmakers?apiKey=do-not-print")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    message = sanitized_error(error)
    assert message == "OddsPapi HTTP 401"
    assert "do-not-print" not in message


def test_oddspapi_bulk_dump_preserves_fixture_records(tmp_path: Path) -> None:
    dump_path = tmp_path / "oddspapi_discovered_events.json"
    SportsOddsConnector._write_discovery_dump(
        dump_path,
        {"record_count": 1, "records": [{"fixtureId": "fixture-1"}]},
    )

    SportsOddsConnector._append_bulk_dump(
        dump_path,
        {
            "tournament_ids": ["17"],
            "response_count": 1,
            "responses": [
                {
                    "bookmaker": "pinnacle",
                    "status": "success",
                    "response": {"fixtureId": "fixture-1", "bookmakerOdds": {}},
                }
            ],
        },
    )

    document = json.loads(dump_path.read_text(encoding="utf-8"))
    assert document["records"][0]["fixtureId"] == "fixture-1"
    assert document["odds_by_tournaments"]["responses"][0]["bookmaker"] == "pinnacle"

    SportsOddsConnector._write_fixture_dump_preserving_bulk(
        dump_path,
        {"record_count": 1, "records": [{"fixtureId": "fixture-2"}]},
    )
    next_scan = json.loads(dump_path.read_text(encoding="utf-8"))
    assert next_scan["records"][0]["fixtureId"] == "fixture-2"
    assert next_scan["odds_by_tournaments"]["responses"][0]["bookmaker"] == "pinnacle"


def test_oddspapi_recognizable_bookmaker_outcome_must_agree_with_catalog() -> None:
    assert bookmaker_outcome_agrees("away", "away")
    assert bookmaker_outcome_agrees("away", "2")
    assert bookmaker_outcome_agrees("away", "opaque-bookmaker-id")
    assert not bookmaker_outcome_agrees("away", "home")
    assert not bookmaker_outcome_agrees("away", "1")


@pytest.mark.asyncio
async def test_oddspapi_http_failure_raises_credential_safe_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            request=request,
            json={
                "message": "Invalid bookmakers parameter",
                "request": f"{request.url}",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector(
        "do-not-print",
        "https://api.oddspapi.io/v4",
        ["pinnacle"],
        client,
    )

    with pytest.raises(OddsPapiHTTPError, match="OddsPapi HTTP 403") as captured:
        await connector.discover_events(
            datetime(2026, 7, 30, tzinfo=UTC),
            datetime(2026, 7, 31, tzinfo=UTC),
        )

    assert "do-not-print" not in str(captured.value)
    assert "Invalid bookmakers parameter" in str(captured.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_v4_fixture_discovery_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_inline(function: object, *args: object) -> object:
        assert callable(function)
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/fixtures"
        assert request.url.params["apiKey"] == "secret"
        assert request.url.params["sportId"] == "10"
        assert request.url.params["statusId"] == "0"
        assert request.url.params["hasOdds"] == "true"
        assert "bookmakers" not in request.url.params
        assert "bookmaker" not in request.url.params
        assert "from" in request.url.params and "to" in request.url.params
        return httpx.Response(
            200,
            json=[
                {
                    "fixtureId": "fixture-1",
                    "participant1Name": "Inter Miami CF",
                    "participant2Name": "Atlanta United",
                    "tournamentName": "MLS",
                    "tournamentId": 17,
                    "startTime": "2026-07-30T20:00:00.000Z",
                    "statusId": 0,
                    "statusName": "Pre-Game",
                    "sportName": "Soccer",
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector(
        "secret",
        "https://api.oddspapi.io/v4",
        ["bookmaker_eu", "pinnacle"],
        client,
        discovery_dump_path=str(tmp_path / "oddspapi-events.json"),
    )
    connector.use_provider_bookmaker_ids(
        [
            Bookmaker(
                canonical_id="bookmaker_eu",
                display_name="BookMaker.eu",
                provider_bookmaker_id="bookmaker.eu",
                availability_status=AvailabilityStatus.AVAILABLE,
            ),
            Bookmaker(
                canonical_id="pinnacle",
                display_name="Pinnacle",
                provider_bookmaker_id="pinnacle-sports",
                availability_status=AvailabilityStatus.AVAILABLE,
            ),
        ],
        ["bookmaker_eu", "pinnacle"],
    )
    start = datetime(2026, 7, 30, 12, tzinfo=UTC)
    events = await connector.discover_events(start, start + timedelta(hours=24))
    assert events[0].provider_event_id == "fixture-1"
    assert events[0].title == "Inter Miami CF vs Atlanta United"
    assert events[0].provider_competition_id == "17"
    assert events[0].scheduled_start == datetime(2026, 7, 30, 20, tzinfo=UTC)
    dump = json.loads((tmp_path / "oddspapi-events.json").read_text())
    assert dump["record_count"] == 1
    assert dump["bookmaker_filter"] is None
    assert dump["configured_bookmaker_slugs"] == ["bookmaker.eu", "pinnacle-sports"]
    assert dump["records"][0]["fixtureId"] == "fixture-1"
    assert "apiKey" not in json.dumps(dump)
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_accepts_configured_slugs_confirmed_by_live_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/bookmakers"
        return httpx.Response(
            200,
            json=[
                {"slug": "pinnacle", "bookmakerName": "Pinnacle", "active": True},
                {"slug": "leovegas", "bookmakerName": "LeoVegas", "active": True},
                {"slug": "888sport", "bookmakerName": "888sport", "active": True},
                {"slug": "unconfigured", "bookmakerName": "Other Book", "active": True},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector(
        "secret",
        "https://api.oddspapi.io/v4",
        ["pinnacle", "leovegas", "888sport"],
        client,
    )

    mapped, unknown = await connector.list_bookmakers()

    assert {item.canonical_id for item in mapped} == {
        "pinnacle",
        "leovegas",
        "888sport",
    }
    assert {item.provider_bookmaker_id for item in mapped} == {
        "pinnacle",
        "leovegas",
        "888sport",
    }
    assert unknown == ["Other Book (unconfigured)"]
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_v4_odds_contract_and_nested_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4/markets":
            return httpx.Response(
                200,
                json=[
                    {
                        "marketId": 101,
                        "marketName": "Full Time Result",
                        "playerProp": False,
                        "sportId": 10,
                        "period": "fulltime",
                        "marketType": "1x2",
                        "outcomes": [{"outcomeId": 102, "outcomeName": "1"}],
                    }
                ],
            )
        assert request.url.path == "/v4/odds"
        assert request.url.params["fixtureId"] == "fixture-1"
        assert "bookmakers" not in request.url.params
        return httpx.Response(
            200,
            json={
                "bookmakerOdds": {
                    "bookmaker.eu": {
                        "markets": {
                            "101": {
                                "marketActive": True,
                                "outcomes": {
                                    "102": {
                                        "players": {
                                            "0": {
                                                "active": True,
                                                "bookmakerOutcomeId": "home",
                                                "changedAt": "2026-07-30T12:00:00.000Z",
                                                "price": 2.15,
                                                "mainLine": True,
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector(
        "secret", "https://api.oddspapi.io/v4", ["bookmaker_eu"], client
    )
    connector.use_provider_bookmaker_ids(
        [
            Bookmaker(
                canonical_id="bookmaker_eu",
                display_name="BookMaker.eu",
                provider_bookmaker_id="bookmaker.eu",
                availability_status=AvailabilityStatus.AVAILABLE,
            )
        ],
        ["bookmaker_eu"],
    )
    quotes = await connector.get_event_odds("fixture-1")
    assert quotes[0].bookmaker_id == "bookmaker_eu"
    assert quotes[0].market_id == 101
    assert quotes[0].provider_outcome_id == 102
    assert quotes[0].decimal_odds == Decimal("2.15")
    assert quotes[0].changed_at == datetime(2026, 7, 30, 12, tzinfo=UTC)
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_bulk_odds_contract_filters_unrequested_fixtures() -> None:
    requested_paths: list[str] = []

    def odds_payload(fixture_id: str) -> dict[str, object]:
        return {
            "fixtureId": fixture_id,
            "bookmakerOdds": {
                "bookmaker.eu": {
                    "markets": {
                        "101": {
                            "marketActive": True,
                            "outcomes": {
                                "102": {
                                    "players": {
                                        "0": {
                                            "active": True,
                                            "bookmakerOutcomeId": "home",
                                            "changedAt": "2026-07-30T12:00:00.000Z",
                                            "price": 2.15,
                                            "mainLine": True,
                                        }
                                    }
                                }
                            },
                        }
                    }
                }
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v4/markets":
            return httpx.Response(
                200,
                json=[
                    {
                        "marketId": 101,
                        "playerProp": False,
                        "sportId": 10,
                        "period": "fulltime",
                        "marketType": "1x2",
                        "outcomes": [{"outcomeId": 102, "outcomeName": "1"}],
                    }
                ],
            )
        assert request.url.path == "/v4/odds-by-tournaments"
        assert request.url.params["tournamentIds"] == "17,8"
        assert request.url.params["bookmaker"] == "bookmaker.eu"
        assert "bookmakers" not in request.url.params
        assert request.url.params["language"] == "en"
        assert request.url.params["verbosity"] == "3"
        assert request.url.params["oddsFormat"] == "decimal"
        return httpx.Response(
            200,
            json=[odds_payload("fixture-1"), odds_payload("unrequested-fixture")],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector(
        "secret", "https://api.oddspapi.io/v4", ["bookmaker_eu"], client
    )
    connector.use_provider_bookmaker_ids(
        [
            Bookmaker(
                canonical_id="bookmaker_eu",
                display_name="BookMaker.eu",
                provider_bookmaker_id="bookmaker.eu",
                availability_status=AvailabilityStatus.AVAILABLE,
            )
        ],
        ["bookmaker_eu"],
    )
    events = [
        ProviderEvent(
            provider=Provider.ODDSPAPI,
            provider_event_id=f"fixture-{index}",
            title=f"Home {index} vs Away {index}",
            status="Pre-Game",
            provider_competition_id=tournament_id,
        )
        for index, tournament_id in ((1, "17"), (2, "8"))
    ]

    quotes_by_fixture = await connector.get_events_odds(events)

    assert requested_paths == ["/v4/markets", "/v4/odds-by-tournaments"]
    assert len(quotes_by_fixture["fixture-1"]) == 1
    assert quotes_by_fixture["fixture-2"] == []
    assert "unrequested-fixture" not in quotes_by_fixture
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_bulk_odds_chunks_tournaments_at_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tournament_requests: list[str] = []

    async def skip_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", skip_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4/markets":
            return httpx.Response(200, json=[])
        assert request.url.path == "/v4/odds-by-tournaments"
        assert request.url.params["bookmaker"] == "pinnacle"
        tournament_requests.append(request.url.params["tournamentIds"])
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector("secret", "https://api.oddspapi.io/v4", ["pinnacle"], client)
    connector.use_provider_bookmaker_ids(
        [
            Bookmaker(
                canonical_id="pinnacle",
                display_name="Pinnacle",
                provider_bookmaker_id="pinnacle",
                availability_status=AvailabilityStatus.AVAILABLE,
            )
        ],
        ["pinnacle"],
    )
    events = [
        ProviderEvent(
            provider=Provider.ODDSPAPI,
            provider_event_id=f"fixture-{index}",
            title=f"Home {index} vs Away {index}",
            status="Pre-Game",
            provider_competition_id=str(index),
        )
        for index in range(1, 7)
    ]

    await connector.get_events_odds(events)

    assert tournament_requests == ["1,2,3,4,5", "6"]
    await client.aclose()


@pytest.mark.asyncio
async def test_oddspapi_bulk_odds_retries_rate_limited_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_attempts = 0
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bulk_attempts
        if request.url.path == "/v4/markets":
            return httpx.Response(200, json=[])
        bulk_attempts += 1
        if bulk_attempts == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "You are being rate limited.",
                        "code": "RATE_LIMITED",
                        "retryMs": 133,
                    }
                },
            )
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = SportsOddsConnector("secret", "https://api.oddspapi.io/v4", ["kalshi"], client)
    connector.use_provider_bookmaker_ids(
        [
            Bookmaker(
                canonical_id="kalshi",
                display_name="Kalshi",
                provider_bookmaker_id="kalshi",
                availability_status=AvailabilityStatus.AVAILABLE,
            )
        ],
        ["kalshi"],
    )
    event = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="fixture-1",
        title="Home vs Away",
        status="Pre-Game",
        provider_competition_id="21",
    )

    await connector.get_events_odds([event])

    assert bulk_attempts == 2
    assert delays == [pytest.approx(1.05)]
    await client.aclose()
