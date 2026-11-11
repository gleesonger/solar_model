"""Separate model compilation from Numba's per-process support compilation."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from typing import Any


@dataclass
class NativeCompilation:
    model_seconds: float = 0.
    support_seconds: float = 0.
    disk_hits: int = 0
    disk_misses: int = 0
    cache: str = "python"


@contextmanager
def track_native_compilation(enabled: bool) -> Iterator[NativeCompilation]:
    """Observe real compilation/cache activity without warming or running a model."""
    metrics = NativeCompilation()
    if not enabled:
        yield metrics
        return

    from numba.core.event import TimingListener, install_listener
    from solar_model.planning.markov import compiled_functions

    functions = compiled_functions()
    before_hits = sum(sum(function.stats.cache_hits.values()) for function in functions)
    before_misses = sum(sum(function.stats.cache_misses.values()) for function in functions)
    loaded = any(function.signatures for function in functions)

    class ModelTimer(TimingListener):
        def on_start(self, event: Any) -> None:
            if event.data["dispatcher"] in functions:
                super().on_start(event)

        def on_end(self, event: Any) -> None:
            if event.data["dispatcher"] in functions:
                super().on_end(event)

    all_compilation, model_compilation = TimingListener(), ModelTimer()
    try:
        with ExitStack() as stack:
            stack.enter_context(install_listener("numba:compile", all_compilation))
            stack.enter_context(install_listener("numba:compile", model_compilation))
            yield metrics
    finally:
        total = all_compilation.duration if all_compilation.done else 0.
        metrics.model_seconds = model_compilation.duration if model_compilation.done else 0.
        metrics.support_seconds = max(0., total - metrics.model_seconds)
        metrics.disk_hits = sum(sum(function.stats.cache_hits.values()) for function in functions) - before_hits
        metrics.disk_misses = sum(sum(function.stats.cache_misses.values()) for function in functions) - before_misses
        if metrics.disk_misses:
            metrics.cache = "mixed" if metrics.disk_hits else "compiled"
        elif metrics.disk_hits:
            metrics.cache = "disk"
        else:
            metrics.cache = "memory" if loaded else "unused"
