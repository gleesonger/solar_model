"""Private demonstrations have bounded admission and an independent worker."""
import asyncio
from types import SimpleNamespace

import pytest

from solar_model.config import load_config
from solar_model.dashboard import planning_worker


def test_second_demo_is_rejected_instead_of_queued(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def io(callback):
            started.set()
            await release.wait()
            worker.running = False
            return {"ok": True, "report": object()}
        worker = SimpleNamespace(running=False, submit=lambda *args: setattr(worker, "running", True), wait=lambda: None, close=lambda: setattr(worker, "running", False))
        monkeypatch.setattr(planning_worker, "_worker", worker)
        monkeypatch.setattr(planning_worker, "_busy", False)
        monkeypatch.setattr(planning_worker.run, "io_bound", io)
        task = asyncio.create_task(planning_worker.demonstrate(load_config()))
        await started.wait()
        with pytest.raises(RuntimeError, match="Another demonstration"):
            await planning_worker.demonstrate(load_config())
        release.set()
        assert (await task)["ok"]
    asyncio.run(scenario())
