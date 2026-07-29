from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def event_time_rejections(
    kickoff_utc: datetime,
    now_utc: datetime,
    client_timezone: str,
    minimum_buffer_minutes: int,
) -> list[str]:
    zone = ZoneInfo(client_timezone)
    localized_kickoff = kickoff_utc.astimezone(zone)
    localized_now = now_utc.astimezone(zone)
    reasons: list[str] = []
    if localized_kickoff.date() != localized_now.date():
        reasons.append("not_same_day")
    if kickoff_utc <= now_utc:
        reasons.append("event_started")
    elif kickoff_utc - now_utc < timedelta(minutes=minimum_buffer_minutes):
        reasons.append("kickoff_too_close")
    return reasons
