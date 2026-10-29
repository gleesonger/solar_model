from __future__ import annotations
from collections.abc import Iterable
from typing import Any, Callable, TypeVar
T = TypeVar("T")


def table_to_object_list(class_type: type[T], *args: Iterable[Any]) -> list[T]:
    """Construct one object per row, unpacking each row as positional arguments."""
    return [class_type(*row) for row in args]


def try_get(operation: Callable[[], T], default: T, exception_type: type[Exception]) -> T:
    """Return an operation's result, or default when exception_type is raised."""
    try:
        return operation()
    except exception_type:
        return default

def str_is_null_or_empty(value: str | None) -> bool:
    return value is None or value.strip() == ""
