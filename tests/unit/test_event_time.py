from datetime import UTC, datetime, timedelta, timezone

from app.domain.enums import Provider
from app.domain.models import CanonicalEvent
from app.providers.records import ProviderEvent
from app.services.event_time import event_time_rejections


def reasons(kickoff: datetime, now: datetime) -> list[str]:
    return event_time_rejections(kickoff, now, 10)


def test_today_passes_and_buffer_is_enforced() -> None:
    now = datetime(2026, 7, 29, 16, tzinfo=UTC)
    assert reasons(now + timedelta(hours=2), now) == []
    assert "kickoff_too_close" in reasons(now + timedelta(minutes=9), now)


def test_future_days_are_allowed_and_started_events_fail() -> None:
    now = datetime(2026, 7, 29, 16, tzinfo=UTC)
    assert reasons(now + timedelta(days=1), now) == []
    yesterday = reasons(now - timedelta(days=1), now)
    assert yesterday == ["event_started"]
    assert "event_started" in reasons(now - timedelta(minutes=1), now)


def test_utc_date_boundary_does_not_reject_future_event() -> None:
    now = datetime(2026, 7, 30, 0, 30, tzinfo=UTC)  # July 29 in New York
    kickoff = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    assert reasons(kickoff, now) == []


def test_daylight_saving_has_no_effect_on_utc_business_logic() -> None:
    now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    kickoff = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    assert reasons(kickoff, now) == []


def test_domain_and_provider_boundaries_normalize_offsets_to_utc() -> None:
    offset_time = datetime(2026, 8, 3, 9, tzinfo=timezone(timedelta(hours=-7)))
    canonical = CanonicalEvent(
        competition="MLS",
        home_team="Inter Miami",
        away_team="Atlanta United",
        kickoff_time_utc=offset_time,
    )
    provider = ProviderEvent(
        provider=Provider.ODDSPAPI,
        provider_event_id="fixture-1",
        title="Inter Miami vs Atlanta United",
        status="Pre-Game",
        scheduled_start=offset_time,
    )

    expected = datetime(2026, 8, 3, 16, tzinfo=UTC)
    assert canonical.kickoff_time_utc == expected
    assert provider.scheduled_start == expected
