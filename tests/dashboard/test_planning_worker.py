"""The planning worker can be started and reused without any database inputs."""
import os
from time import perf_counter, sleep
from types import SimpleNamespace

import cvxpy  # Load native solver dependencies before pandas/NiceGUI on Windows.

from solar_model.services.planning import warm_planning_worker


def timed_worker_job():
    began_at = perf_counter()
    sleep(.05)
    return os.getpid(), began_at, perf_counter()


def test_spawned_planning_worker_warms_and_reuses_process(tmp_path, monkeypatch):
    from solar_model.dashboard import planning_worker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(planning_worker, "_planning_pool", None)
    shutdowns = []
    previous = SimpleNamespace(shutdown=lambda **kwargs: shutdowns.append(kwargs))
    monkeypatch.setattr(planning_worker.run, "process_pool", previous)
    with planning_worker.configure_planning_worker() as pool:
        assert planning_worker.run.process_pool is pool
        assert planning_worker.configure_planning_worker() is pool
        first = pool.submit(warm_planning_worker).result(timeout=30)
        second = pool.submit(warm_planning_worker).result(timeout=30)
        jobs = [pool.submit(timed_worker_job) for _ in range(2)]
        runs = [job.result(timeout=10) for job in jobs]
    assert first != os.getpid()
    assert second == first
    assert runs[0][0] == runs[1][0] == first
    assert runs[1][1] >= runs[0][2]
    assert shutdowns == [{"wait": False, "cancel_futures": True}]
    assert list(tmp_path.iterdir()) == []


def test_early_requests_share_warmup_and_cancellation_does_not_stop_it(monkeypatch):
    import asyncio
    from solar_model.dashboard import planning_worker
    monkeypatch.setattr(planning_worker, "_warmup_task", None)
    monkeypatch.setattr(planning_worker, "configure_planning_worker", lambda: None)
    calls = []
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def warm(callback):
            calls.append(callback)
            started.set()
            await release.wait()
            return 123
        monkeypatch.setattr(planning_worker.run, "cpu_bound", warm)
        startup = asyncio.create_task(planning_worker.ensure_planning_worker())
        await started.wait()
        request = asyncio.create_task(planning_worker.ensure_planning_worker())
        await asyncio.sleep(0)
        assert not request.done()
        assert len(calls) == 1
        startup.cancel()
        try:
            await startup
        except asyncio.CancelledError:
            pass
        assert not planning_worker._warmup_task.cancelled()
        release.set()
        await request
        await planning_worker.ensure_planning_worker()
        assert len(calls) == 1
    asyncio.run(scenario())


def test_failed_warmup_does_not_block_dashboard_or_later_plan_attempt(monkeypatch, caplog):
    import asyncio
    from solar_model.dashboard import planning_worker
    monkeypatch.setattr(planning_worker, "_warmup_task", None)
    monkeypatch.setattr(planning_worker, "configure_planning_worker", lambda: None)
    async def fail(callback):
        raise RuntimeError("synthetic warm-up failure")
    monkeypatch.setattr(planning_worker.run, "cpu_bound", fail)
    async def scenario():
        await planning_worker.ensure_planning_worker()
        await planning_worker.ensure_planning_worker()
    asyncio.run(scenario())
    assert "Planning worker warm-up failed" in caplog.text
