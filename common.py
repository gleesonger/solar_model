from __future__ import annotations

import logging
from pathlib import Path
import time
from functools import wraps
from datetime import datetime, timezone
from typing import Callable, ParamSpec, TypeVar
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger("solar_model")
LOCAL_ZONE = ZoneInfo("Europe/Dublin")
P = ParamSpec("P")
T = TypeVar("T")


def heavy_work(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Log the start, completion time, and failure of a costly synchronous task."""
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            started_at = time.perf_counter()
            try:
                result = function(*args, **kwargs)
            except Exception:
                elapsed_seconds = time.perf_counter() - started_at
                LOGGER.exception("Heavy work failed: %s (%.3fs)", name, elapsed_seconds)
                raise
            elapsed_seconds = time.perf_counter() - started_at
            LOGGER.info("%s (%.3fs)", name, elapsed_seconds)
            return result

        return wrapped

    return decorate


def configure_logging(level: str) -> None:
    """Configure timestamped process logging."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # DEBUG is useful for our own application and SQL diagnostics, but the
    # Modbus library emits low-level protocol traffic that is not actionable.
    for logger_name in ("pymodbus", "pymodbus.logging"):
        modbus_logger = logging.getLogger(logger_name)
        modbus_logger.disabled = True
        modbus_logger.propagate = False


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

def str_is_null_or_empty(value: str | None) -> bool:
    return value is None or value.strip() == ""
