from __future__ import annotations

import logging
from pathlib import Path
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger("solar_model")
LOCAL_ZONE = ZoneInfo("Europe/Dublin")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamps(now: datetime | None = None, timezone_name: str = "Europe/Dublin") -> tuple[str, str]:
    current = now or utc_now()
    return current.isoformat(timespec="seconds"), current.astimezone(ZoneInfo(timezone_name)).isoformat(timespec="seconds")


def sleep_until_next_interval(interval_seconds: int) -> None:
    now = time.time()
    delay = interval_seconds - (now % interval_seconds)
    time.sleep(delay)


def source_path(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


