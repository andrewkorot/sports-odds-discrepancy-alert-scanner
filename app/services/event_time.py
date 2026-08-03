from datetime import datetime, timedelta


def event_time_rejections(
    kickoff_utc: datetime,
    now_utc: datetime,
    minimum_buffer_minutes: int,
) -> list[str]:
    reasons: list[str] = []
    if kickoff_utc <= now_utc:
        reasons.append("event_started")
    elif kickoff_utc - now_utc < timedelta(minutes=minimum_buffer_minutes):
        reasons.append("kickoff_too_close")
    return reasons
