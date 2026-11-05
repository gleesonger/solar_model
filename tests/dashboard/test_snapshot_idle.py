"""Browser summaries sleep without subscribers and resume promptly on reconnect."""
import asyncio
from types import SimpleNamespace

from solar_model.dashboard import snapshot


def test_snapshots_sleep_without_viewers_and_refresh_on_stale_reconnect(monkeypatch):
    store = snapshot.DashboardSnapshotStore(None, None, 60.)
    clock = SimpleNamespace(now=0.)
    calls = []
    monkeypatch.setattr(snapshot, "perf_counter", lambda: clock.now)

    async def refresh():
        calls.append(clock.now)

    monkeypatch.setattr(store, "refresh", refresh)

    async def settle():
        for _ in range(5):
            await asyncio.sleep(0)

    async def scenario():
        worker = asyncio.create_task(store.run_forever())
        await settle()
        assert calls == []
        unsubscribe = store.subscribe(lambda updated: None)
        await settle()
        assert calls == [0.]
        another = store.subscribe(lambda updated: None)
        await settle()
        assert calls == [0.]
        unsubscribe()
        another()
        await settle()
        clock.now = 100.
        await settle()
        assert calls == [0.]
        unsubscribe = store.subscribe(lambda updated: None)
        await settle()
        assert calls == [0., 100.]
        unsubscribe()
        await settle()
        store.stop()
        await asyncio.wait_for(worker, timeout=1)

    asyncio.run(scenario())
