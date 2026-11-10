"""One on-demand private demonstration worker, separate from scheduled plans."""
from __future__ import annotations

from typing import Any

from nicegui import run

from solar_model.config import Config
from solar_model.planning.types import NoFeasiblePlanError
from solar_model.runtime.planning_worker import PlanningWorker

_worker: PlanningWorker | None = None
_busy = False


async def demonstrate(config: Config) -> dict[str, Any]:
    global _worker, _busy
    if _busy:
        raise RuntimeError("Another demonstration is running; try again when it finishes")
    _busy = True
    try:
        if _worker is None:
            _worker = PlanningWorker()
        _worker.submit("demonstration", config)
        result = await run.io_bound(_worker.wait)
        if not result["ok"]:
            error_type = NoFeasiblePlanError if result.get("error_type") == "NoFeasiblePlanError" else RuntimeError
            raise error_type(result["error"])
        return result
    finally:
        if _worker is not None and _worker.running:
            _worker.close()
        _busy = False


def stop_demonstrations() -> None:
    if _worker is not None:
        _worker.close()
