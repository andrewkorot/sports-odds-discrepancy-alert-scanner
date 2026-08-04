from datetime import time
from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    Bookmaker,
    CanonicalEvent,
    ConnectorHealth,
    EventMatchAudit,
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
    last_scan_error: str | None = None
    scan_in_progress: bool = False
    scanning_enabled: bool = True
    scan_control_source: str = "startup"
    auto_start_stop_enabled: bool = False


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    scanner = state(request)
    database_status, redis_status = await orchestrator(request).infrastructure_health()
    provider_records = await orchestrator(request).health()
    coverage = {b.canonical_id: b.availability_status.value for b in scanner.bookmakers}
    return HealthResponse(
        application_status=(
            "ok"
            if database_status in {"ok", "mock"} and redis_status in {"ok", "mock"}
            else "degraded"
        ),
        database_status=database_status,
        redis_status=redis_status,
        provider_statuses={
            record.provider.value: (
                "connected" if record.connected else ("disabled" if not record.enabled else "error")
            )
            for record in provider_records
        },
        required_bookmaker_coverage=coverage,
        last_successful_update=scanner.last_updated.isoformat() if scanner.last_updated else None,
        last_scan_error=orchestrator(request).last_scan_error,
        scan_in_progress=orchestrator(request).scan_in_progress,
        scanning_enabled=orchestrator(request).scanning_enabled,
        scan_control_source=orchestrator(request).scan_control_source,
        auto_start_stop_enabled=scanner.settings.auto_start_stop_enabled,
    )


class ScannerControlResponse(BaseModel):
    scanning_enabled: bool
    scan_in_progress: bool
    control_source: str
    auto_start_stop_enabled: bool
    auto_start_time: str
    auto_stop_time: str
    timezone: str


def scanner_control_response(request: Request) -> ScannerControlResponse:
    controller = orchestrator(request)
    settings = state(request).settings
    return ScannerControlResponse(
        scanning_enabled=controller.scanning_enabled,
        scan_in_progress=controller.scan_in_progress,
        control_source=controller.scan_control_source,
        auto_start_stop_enabled=settings.auto_start_stop_enabled,
        auto_start_time=settings.scan_auto_start_time.strftime("%H:%M"),
        auto_stop_time=settings.scan_auto_stop_time.strftime("%H:%M"),
        timezone=settings.client_timezone,
    )


@router.get("/scanner/control", response_model=ScannerControlResponse)
async def scanner_control(request: Request) -> ScannerControlResponse:
    return scanner_control_response(request)


@router.post("/scanner/start", response_model=ScannerControlResponse)
async def start_scanner(request: Request) -> ScannerControlResponse:
    await orchestrator(request).manual_start()
    return scanner_control_response(request)


@router.post("/scanner/stop", response_model=ScannerControlResponse)
async def stop_scanner(request: Request) -> ScannerControlResponse:
    await orchestrator(request).manual_stop()
    return scanner_control_response(request)


class RuntimeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_kalshi_ask_size: Decimal | None = Field(default=None, ge=0)
    min_polymarket_ask_size: Decimal | None = Field(default=None, ge=0)
    discovery_calendar_days: int | None = Field(default=None, ge=1)
    alert_cooldown_minutes: int | None = Field(default=None, ge=0)
    realert_edge_increase_pp: Decimal | None = Field(default=None, ge=0)
    min_minutes_before_kickoff: int | None = Field(default=None, ge=0)
    event_match_kickoff_tolerance_minutes: int | None = Field(default=None, ge=0, le=180)
    max_prediction_price_age_seconds: int | None = Field(default=None, ge=1)
    max_sportsbook_price_age_seconds: int | None = Field(default=None, ge=1)
    max_bid_ask_spread_cents: Decimal | None = Field(default=None, ge=0)
    depth_window_from_midpoint_cents: Decimal | None = Field(default=None, ge=0)
    min_depth_within_window_usd: Decimal | None = Field(default=None, ge=0)
    min_trailing_24h_volume_usd: Decimal | None = Field(default=None, ge=0)
    edge_threshold_pp: Decimal | None = Field(default=None, ge=0)
    price_poll_interval_seconds: int | None = Field(default=None, ge=5, le=86400)
    provider_request_concurrency: int | None = Field(default=None, ge=1, le=32)
    auto_start_stop_enabled: bool | None = None
    scan_auto_start_time: time | None = None
    scan_auto_stop_time: time | None = None
    event_match_fuzzy_min_score: Decimal | None = Field(default=None, ge=0, le=100)
    event_match_ambiguity_margin: Decimal | None = Field(default=None, ge=0, le=100)


@router.get("/bookmakers", response_model=list[Bookmaker])
async def bookmakers(request: Request) -> list[Bookmaker]:
    return state(request).bookmakers


@router.get("/events", response_model=list[CanonicalEvent])
async def events(request: Request, sport: str | None = None) -> list[CanonicalEvent]:
    return [
        item for item in state(request).events if sport is None or item.sport == sport.casefold()
    ]


@router.get("/event-matches", response_model=list[EventMatchAudit])
async def event_matches(
    request: Request,
    matched: bool | None = None,
    provider: str | None = None,
    confidence: str | None = None,
) -> list[EventMatchAudit]:
    return [
        item
        for item in state(request).event_matches
        if (matched is None or item.matched is matched)
        and (provider is None or item.provider.value == provider.casefold())
        and (confidence is None or item.match_confidence.value == confidence.casefold())
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
        "event_match_kickoff_tolerance_minutes": (current.event_match_kickoff_tolerance_minutes),
        "event_match_fuzzy_min_score": current.event_match_fuzzy_min_score,
        "event_match_ambiguity_margin": current.event_match_ambiguity_margin,
        "discovery_calendar_days": current.discovery_calendar_days,
        "alert_cooldown_minutes": current.alert_cooldown_minutes,
        "realert_edge_increase_pp": current.realert_edge_increase_pp,
        "app_mode": current.app_mode,
        "kalshi_mode": current.kalshi_mode,
        "polymarket_mode": current.polymarket_mode,
        "sports_odds_mode": current.sports_odds_mode,
        "live_dry_run": current.live_dry_run,
        "alerts_enabled": current.alerts_enabled,
        "price_poll_interval_seconds": current.price_poll_interval_seconds,
        "auto_start_stop_enabled": current.auto_start_stop_enabled,
        "scan_auto_start_time": current.scan_auto_start_time.strftime("%H:%M"),
        "scan_auto_stop_time": current.scan_auto_stop_time.strftime("%H:%M"),
        "provider_request_concurrency": current.provider_request_concurrency,
        "client_timezone": current.client_timezone,
        "enabled_market_types": current.enabled_market_types,
        "enabled_sports": current.enabled_sports,
        "max_bid_ask_spread_cents": current.max_bid_ask_spread_cents,
        "depth_window_from_midpoint_cents": current.depth_window_from_midpoint_cents,
        "min_depth_within_window_usd": current.min_depth_within_window_usd,
        "min_trailing_24h_volume_usd": current.min_trailing_24h_volume_usd,
    }


@router.patch("/settings")
async def update_settings(request: Request, update: RuntimeSettingsUpdate) -> dict[str, object]:
    updates = update.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="At least one runtime setting is required")
    try:
        return await orchestrator(request).update_runtime_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
