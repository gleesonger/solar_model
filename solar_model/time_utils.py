from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time

LOCAL_ZONE = ZoneInfo("Europe/Dublin")

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_instant(value: datetime) -> datetime:
    """Normalize an aware time for elapsed arithmetic, including repeated hours."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(timezone.utc)

def add_elapsed(value: datetime, delta: timedelta) -> datetime:
    return (utc_instant(value) + delta).astimezone(value.tzinfo)

def timestamps(now: datetime | None = None, timezone_name: str = "Europe/Dublin") -> tuple[str, str]:
    current = now or utc_now()
    return current.isoformat(timespec="seconds"), current.astimezone(ZoneInfo(timezone_name)).isoformat(timespec="seconds")

def sleep_until_next_interval(interval_seconds: int) -> None:
    now = time.time()
    delay = interval_seconds - (now % interval_seconds)
    time.sleep(delay)
