from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    ConnectorHealth,
    MarketCandidate,
    Opportunity,
    PredictionMarketQuote,
)
from app.providers.records import ProviderHealthRecord
from app.services.orchestration import ScanOrchestrator
from app.services.scanner import ScannerState

router = APIRouter()


def state(request: Request) -> ScannerState:
    return cast(ScannerState, request.app.state.scanner)


def orchestrator(request: Request) -> ScanOrchestrator:
    return cast(ScanOrchestrator, request.app.state.orchestrator)


class HealthResponse(BaseModel):
    application_status: str
    database_status: str
    redis_status: str
    provider_statuses: dict[str, str]
    required_bookmaker_coverage: dict[str, str]
    last_successful_update: str | None


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    scanner = state(request)
    coverage = {b.canonical_id: b.availability_status.value for b in scanner.bookmakers}
    return HealthResponse(
        application_status="ok",
        database_status="mock" if scanner.settings.mock_mode else "configured",
        redis_status="mock" if scanner.settings.mock_mode else "configured",
        provider_statuses={"kalshi": "mock", "polymarket": "mock", "oddspapi": "mock"},
        required_bookmaker_coverage=coverage,
        last_successful_update=scanner.last_updated.isoformat() if scanner.last_updated else None,
    )


@router.get("/bookmakers", response_model=list[Bookmaker])
async def bookmakers(request: Request) -> list[Bookmaker]:
    return state(request).bookmakers


@router.get("/events", response_model=list[CanonicalEvent])
async def events(request: Request, sport: str | None = None) -> list[CanonicalEvent]:
    return [
        item
        for item in state(request).events
        if sport is None or item.sport == sport.casefold()
    ]


@router.get("/opportunities", response_model=list[Opportunity])
async def opportunities(
    request: Request,
    bookmaker: str | None = None,
    prediction_market: str | None = None,
    competition: str | None = None,
    minimum_edge: Annotated[Decimal | None, Query(ge=0)] = None,
    active_only: bool = True,
    market_type: str | None = None,
    sport: str | None = None,
) -> list[Opportunity]:
    values = state(request).opportunities
    return [
        item
        for item in values
        if (bookmaker is None or item.bookmaker_id == bookmaker)
        and (sport is None or item.sport == sport.casefold())
        and (market_type is None or item.market_type.value == market_type)
        and (
            prediction_market is None or item.prediction_market_provider.value == prediction_market
        )
        and (competition is None or item.competition.casefold() == competition.casefold())
        and (minimum_edge is None or item.edge_percentage_points >= minimum_edge)
        and (not active_only or item.active)
    ]


@router.get("/market-candidates", response_model=list[MarketCandidate])
async def market_candidates(
    request: Request,
    market_type: str | None = None,
    accepted: bool | None = None,
    rejection_reason: str | None = None,
    provider: str | None = None,
    sport: str | None = None,
) -> list[MarketCandidate]:
    return [
        item
        for item in state(request).candidates
        if (market_type is None or item.prediction_quote.market_type.value == market_type)
        and (sport is None or item.prediction_quote.sport == sport.casefold())
        and (accepted is None or item.accepted is accepted)
        and (rejection_reason is None or rejection_reason in item.rejection_reasons)
        and (provider is None or item.prediction_quote.provider.value == provider)
    ]


@router.get("/market-candidates/{candidate_id}", response_model=MarketCandidate)
async def market_candidate(request: Request, candidate_id: UUID) -> MarketCandidate:
    for item in state(request).candidates:
        if item.id == candidate_id:
            return item
    raise HTTPException(status_code=404, detail="Market candidate not found")


@router.get("/opportunities/{opportunity_id}", response_model=Opportunity)
async def opportunity(request: Request, opportunity_id: UUID) -> Opportunity:
    for item in state(request).opportunities:
        if item.id == opportunity_id:
            return item
    raise HTTPException(status_code=404, detail="Opportunity not found")


@router.get("/settings")
async def settings(request: Request) -> dict[str, object]:
    current = state(request).settings
    return {
        "app_env": current.app_env,
        "mock_mode": current.mock_mode,
        "enabled_bookmakers": current.enabled_bookmakers,
        "edge_threshold_pp": current.edge_threshold_pp,
        "max_prediction_price_age_seconds": current.max_prediction_price_age_seconds,
        "max_sportsbook_price_age_seconds": current.max_sportsbook_price_age_seconds,
        "min_kalshi_ask_size": current.min_kalshi_ask_size,
        "min_polymarket_ask_size": current.min_polymarket_ask_size,
        "min_minutes_before_kickoff": current.min_minutes_before_kickoff,
        "max_hours_before_kickoff": current.max_hours_before_kickoff,
        "alert_cooldown_minutes": current.alert_cooldown_minutes,
        "realert_edge_increase_pp": current.realert_edge_increase_pp,
        "oddspapi_poll_interval_seconds": current.oddspapi_poll_interval_seconds,
        "app_mode": current.app_mode,
        "kalshi_mode": current.kalshi_mode,
        "polymarket_mode": current.polymarket_mode,
        "sports_odds_mode": current.sports_odds_mode,
        "live_dry_run": current.live_dry_run,
        "alerts_enabled": current.alerts_enabled,
        "telegram_enabled": current.telegram_enabled,
        "price_poll_interval_seconds": current.price_poll_interval_seconds,
        "client_timezone": current.client_timezone,
        "enabled_market_types": current.enabled_market_types,
        "enabled_sports": current.enabled_sports,
        "max_bid_ask_spread_cents": current.max_bid_ask_spread_cents,
        "depth_window_from_midpoint_cents": current.depth_window_from_midpoint_cents,
        "min_depth_within_window_usd": current.min_depth_within_window_usd,
        "min_trailing_24h_volume_usd": current.min_trailing_24h_volume_usd,
    }


@router.get("/connector-health", response_model=list[ConnectorHealth])
async def connector_health(request: Request) -> list[ConnectorHealth]:
    scanner = state(request)
    missing = [b.canonical_id for b in scanner.bookmakers if b.availability_status != "available"]
    return [
        ConnectorHealth(
            provider=provider,
            connection_status="mock_healthy",
            last_successful_request=scanner.last_updated,
            last_data_timestamp=scanner.last_updated,
            missing_required_bookmakers=missing if provider == "oddspapi" else [],
        )
        for provider in ("kalshi", "polymarket", "oddspapi")
    ]


@router.get("/health/providers")
async def provider_health(request: Request) -> list[ProviderHealthRecord]:
    return list(await orchestrator(request).health())


@router.get("/markets")
async def markets(request: Request, sport: str | None = None) -> list[PredictionMarketQuote]:
    return [
        item
        for item in state(request).predictions
        if sport is None or item.sport == sport.casefold()
    ]


@router.get("/matches")
async def matches(request: Request) -> list[dict[str, object]]:
    return [
        {
            "canonical_event_id": event.id,
            "sport": event.sport,
            "competition": event.competition,
            "home_team": event.home_team,
            "away_team": event.away_team,
            "kickoff": event.kickoff_time_utc,
            "match_status": "matched",
        }
        for event in state(request).events
    ]
