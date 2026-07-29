from datetime import UTC, datetime, timedelta

from app.services.event_time import event_time_rejections


def reasons(kickoff: datetime, now: datetime) -> list[str]:
    return event_time_rejections(kickoff, now, "America/New_York", 10)


def test_today_passes_and_buffer_is_enforced() -> None:
    now = datetime(2026, 7, 29, 16, tzinfo=UTC)
    assert reasons(now + timedelta(hours=2), now) == []
    assert "kickoff_too_close" in reasons(now + timedelta(minutes=9), now)


def test_tomorrow_yesterday_and_started_fail() -> None:
    now = datetime(2026, 7, 29, 16, tzinfo=UTC)
    assert "not_same_day" in reasons(now + timedelta(days=1), now)
    yesterday = reasons(now - timedelta(days=1), now)
    assert {"not_same_day", "event_started"} <= set(yesterday)
    assert "event_started" in reasons(now - timedelta(minutes=1), now)


def test_utc_date_can_differ_while_local_date_is_today() -> None:
    now = datetime(2026, 7, 30, 0, 30, tzinfo=UTC)  # July 29 in New York
    kickoff = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    assert reasons(kickoff, now) == []


def test_daylight_saving_boundary_uses_zone_rules() -> None:
    now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    kickoff = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    assert reasons(kickoff, now) == []
