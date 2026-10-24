from __future__ import annotations
import logging
import time
from functools import wraps
from typing import Callable, ParamSpec, TypeVar
LOGGER = logging.getLogger("solar_model")
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
