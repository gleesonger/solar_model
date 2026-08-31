from __future__ import annotations

import logging
from pathlib import Path
import time
from datetime import datetime, timezone
from threading import Lock
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger("solar_model")
LOCAL_ZONE = ZoneInfo("Europe/Dublin")


class ThrottleFilter(logging.Filter):
    """Limit repeated log templates without hiding distinct messages."""

    def __init__(self, interval_seconds: float = 300) -> None:
        super().__init__()
        self._interval_seconds = interval_seconds
        self._last_logged: dict[tuple[str, int, str], float] = {}
        self._lock = Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, str(record.msg))
        now = time.monotonic()
        with self._lock:
            previous = self._last_logged.get(key)
            if previous is not None and now - previous < self._interval_seconds:
                return False
            self._last_logged[key] = now
        return True


def configure_logging(level: str, throttle_seconds: float = 300) -> None:
    """Configure timestamped process logging with repeated-message throttling."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        if not any(isinstance(log_filter, ThrottleFilter) for log_filter in handler.filters):
            handler.addFilter(ThrottleFilter(throttle_seconds))


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


