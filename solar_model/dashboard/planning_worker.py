"""Share asynchronous planning-worker warm-up between startup and plan requests."""
import asyncio
from time import perf_counter

from nicegui import run

from solar_model.logging_setup import LOGGER
from solar_model.services.planning import warm_planning_worker

_warmup_task: asyncio.Task | None = None


async def _warm_worker() -> None:
    begin = perf_counter()
    try:
        pid = await run.cpu_bound(warm_planning_worker)
        LOGGER.info("Planning worker ready: pid=%s startup_seconds=%.3f", pid, perf_counter() - begin)
    except Exception:
        LOGGER.exception("Planning worker warm-up failed; plan requests can still attempt a normal run")


async def ensure_planning_worker() -> None:
    """Await one shared warm-up without blocking the event loop or cancelling it."""
    global _warmup_task
    if _warmup_task is None:
        _warmup_task = asyncio.create_task(_warm_worker())
    await asyncio.shield(_warmup_task)
