from __future__ import annotations
from typing import Callable, TypeVar
T = TypeVar("T")

def try_get(operation: Callable[[], T], default: T, exception_type: type[Exception]) -> T:
    """Return an operation's result, or default when exception_type is raised."""
    try:
        return operation()
    except exception_type:
        return default

def str_is_null_or_empty(value: str | None) -> bool:
    return value is None or value.strip() == ""
