"""Dashboard observes persisted plans; it never schedules shared calculations."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_model.config import load_config, energy_plan_yaml
from solar_model.dashboard import plan_store


@pytest.fixture
def setup_store(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(Path("config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(path)
    store = plan_store.PlanStore(config, str(path))
    stamp = datetime.now(timezone.utc)
    state = SimpleNamespace(id=None, error=None, reads=0)
    async def io(callback, *args):
        return callback(*args)
    def read(*args):
        state.reads += 1
        return SimpleNamespace(report=object(), settings=config.energy_plan, began_at=stamp, full_data_at=stamp, completed_at=stamp, snapshot_id=state.id)
    monkeypatch.setattr(plan_store.run, "io_bound", io)
    monkeypatch.setattr(plan_store, "latest_plan_id", lambda *args: state.id)
    monkeypatch.setattr(plan_store, "read_saved_run", read)
    monkeypatch.setattr(plan_store, "read_planner_status", lambda *args: {"state": "waiting", "updated_at": stamp.isoformat(), "last_error": state.error})
    return store, state


def test_cache_loads_committed_ids_once_and_keeps_previous_on_service_failure(setup_store):
    store, state = setup_store
    notifications = []
    store.subscribe(lambda: notifications.append(store.version))
    async def scenario():
        await store.check_for_update()
        assert store.current is None and state.reads == 0
        state.id = "first"
        await store.check_for_update()
        assert store.current.snapshot_id == "first" and store.version == 1
        await store.check_for_update()
        assert state.reads == 1
        state.error = "synthetic storage failure"
        await store.check_for_update()
        assert store.current.snapshot_id == "first" and store.last_error == state.error
        state.id, state.error = "second", None
        await store.check_for_update()
        assert store.current.snapshot_id == "second" and store.version == 2
    asyncio.run(scenario())
    assert notifications[-1] == 2


def test_live_settings_save_and_invalid_external_changes(setup_store, caplog):
    store, state = setup_store
    settings = replace(store.config.energy_plan, protection_preference=75.)
    store.save_settings(energy_plan_yaml(settings))
    assert store.config.energy_plan == settings
    store.config_path.write_text("energy_plan: [invalid]", encoding="utf-8")
    asyncio.run(store.check_for_update())
    asyncio.run(store.check_for_update())
    assert store.config.energy_plan == settings
    assert caplog.text.count("retaining the last valid settings") == 1


def test_demonstrations_use_separate_worker_without_changing_shared_state(setup_store, monkeypatch):
    store, state = setup_store
    stamp = datetime.now(timezone.utc)
    async def fake(config):
        return {"report": SimpleNamespace(full_data_at=stamp), "began_at": stamp, "completed_at": stamp}
    monkeypatch.setattr(plan_store, "demonstrate", fake)
    async def scenario():
        demo = await store.demonstrate(store.config.energy_plan)
        assert demo.demonstration and demo.snapshot_id is None
        assert store.current is None and store.version == 0
    asyncio.run(scenario())
