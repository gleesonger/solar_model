"""Configure one persistent process for all dashboard planning calculations."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from time import perf_counter

from nicegui import run

from solar_model.logging_setup import LOGGER
from solar_model.services.planning import warm_planning_worker

_warmup_task: asyncio.Task | None = None
_planning_pool: ProcessPoolExecutor | None = None


def configure_planning_worker() -> ProcessPoolExecutor:
    """Replace NiceGUI's unused default pool before the first CPU-bound job."""
    global _planning_pool
    if _planning_pool is not None and run.process_pool is _planning_pool:
        return _planning_pool
    previous = run.process_pool
    _planning_pool = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
    run.process_pool = _planning_pool
    if previous is not None:
        previous.shutdown(wait=False, cancel_futures=True)
    LOGGER.info("Planning process pool configured: workers=1 start_method=spawn")
    return _planning_pool


async def _warm_worker() -> None:
    begin = perf_counter()
    try:
        configure_planning_worker()
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
