"""Shared planning cadence, live settings and browser/background ordering."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import cvxpy  # Load native solver dependencies before NiceGUI on Windows.
import pandas as pd
import pytest

from solar_model.config import energy_plan_yaml, load_config
from solar_model.dashboard import plan_store
from solar_model.dashboard.tabs.plan import PlanTab
from solar_model.planning.types import NoFeasiblePlanError
from solar_model.services.planning import PlanReport
from tests.dashboard.test_plan import sample_result


@pytest.fixture
def setup_store(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(Path("config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(path)
    store = plan_store.PlanStore(config, str(path))
    clock = SimpleNamespace(latest=datetime(2026, 11, 5, 12, tzinfo=timezone.utc), calls=[], failure=False)
    monkeypatch.setattr(plan_store, "latest_full_update", lambda path: clock.latest)
    monkeypatch.setattr(plan_store, "read_saved_run", lambda *args: None)

    async def io_bound(callback, *args):
        return callback(*args)

    async def calculate(config, demonstration=False):
        clock.calls.append(config.energy_plan)
        if clock.failure:
            raise NoFeasiblePlanError("synthetic unavailable plan")
        stamp = clock.latest
        report = PlanReport(sample_result(), pd.DataFrame(), full_data_at=stamp)
        return plan_store.PlanRun(report, config.energy_plan, stamp, stamp, stamp + timedelta(seconds=2), demonstration)

    monkeypatch.setattr(plan_store.run, "io_bound", io_bound)
    monkeypatch.setattr(store, "_calculate", calculate)
    return store, clock


async def finish(store):
    if store.running:
        await asyncio.shield(store._task)


async def start_background(store, clock):
    await store.check_for_update()
    assert store.current is None and not store.running
    clock.latest += timedelta(minutes=1)
    await store.check_for_update()
    await finish(store)


def test_existing_reading_at_startup_does_not_start_a_projection(setup_store):
    store, clock = setup_store

    async def scenario():
        for _ in range(5):
            await store.check_for_update()
            await asyncio.sleep(0)
        assert clock.calls == [] and store.current is None
        clock.latest += timedelta(minutes=1)
        await store.check_for_update()
        await finish(store)
        assert len(clock.calls) == 1

    asyncio.run(scenario())


def test_cadence_uses_new_full_readings_and_exact_tolerance(setup_store):
    store, clock = setup_store

    async def scenario():
        clock.latest = None
        await store.check_for_update()
        assert not store.running
        clock.latest = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        await store.check_for_update()
        await finish(store)
        original = store.current
        assert original.full_data_at == clock.latest
        assert original.began_at < original.completed_at
        await store.check_for_update()
        clock.latest += timedelta(seconds=284)
        await store.check_for_update()
        assert len(clock.calls) == 1
        clock.latest += timedelta(seconds=1)
        await store.check_for_update()
        await finish(store)
        assert len(clock.calls) == 2 and store.version == 2
        assert store.current.full_data_at == clock.latest

    asyncio.run(scenario())


def test_saved_settings_apply_on_next_reading_and_control_later_cadence(setup_store):
    store, clock = setup_store

    async def scenario():
        await start_background(store, clock)
        updated = replace(store.config.energy_plan, plan_rerun_frequency_mins=10., protection_preference=75.)
        store.save_settings(energy_plan_yaml(updated))
        assert store.config.energy_plan == updated
        await store.check_for_update()
        assert len(clock.calls) == 1
        clock.latest += timedelta(seconds=60)
        await store.check_for_update()
        await finish(store)
        assert clock.calls[-1] == updated
        clock.latest += timedelta(seconds=584)
        await store.check_for_update()
        assert len(clock.calls) == 2
        clock.latest += timedelta(seconds=1)
        await store.check_for_update()
        await finish(store)
        assert len(clock.calls) == 3
        # Editing the file outside the browser follows the same live-config path.
        text = store.config_path.read_text(encoding="utf-8").replace("plan_rerun_frequency_mins: 10.0", "plan_rerun_frequency_mins: 2.0")
        store.config_path.write_text(text, encoding="utf-8")
        clock.latest += timedelta(seconds=60)
        await store.check_for_update()
        await finish(store)
        assert clock.calls[-1].plan_rerun_frequency_mins == 2.

    asyncio.run(scenario())


def test_failed_run_preserves_cache_and_retries_only_on_new_data(setup_store, caplog):
    store, clock = setup_store

    async def scenario():
        await start_background(store, clock)
        original = store.current
        clock.failure = True
        clock.latest += timedelta(minutes=5)
        await store.check_for_update()
        await finish(store)
        assert store.current is original and store.version == 1
        assert store.last_error == "synthetic unavailable plan"
        await store.check_for_update()
        assert len(clock.calls) == 2
        clock.failure = False
        clock.latest += timedelta(minutes=1)
        await store.check_for_update()
        await finish(store)
        assert store.version == 2 and store.last_error is None

    asyncio.run(scenario())
    assert all(record.exc_info is None for record in caplog.records)


def test_invalid_external_settings_retain_live_config_and_background_cadence(setup_store, caplog):
    store, clock = setup_store

    async def scenario():
        await start_background(store, clock)
        original = store.config.energy_plan
        store.config_path.write_text("energy_plan: [invalid]", encoding="utf-8")
        clock.latest += timedelta(minutes=5)
        await store.check_for_update()
        await finish(store)
        await store.check_for_update()
        assert store.config.energy_plan == original
        assert len(clock.calls) == 2 and store.version == 2

    asyncio.run(scenario())
    assert caplog.text.count("retaining the last valid settings") == 1


def test_repeated_update_checks_do_not_overlap_background_runs(setup_store, monkeypatch):
    store, clock = setup_store

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        original = store._calculate

        async def wait(config, demonstration=False):
            started.set()
            await release.wait()
            return await original(config, demonstration)

        monkeypatch.setattr(store, "_calculate", wait)
        await store.check_for_update()
        clock.latest += timedelta(minutes=1)
        await store.check_for_update()
        await started.wait()
        original_task = store._task
        clock.latest += timedelta(minutes=5)
        await store.check_for_update()
        assert store._task is original_task
        release.set()
        completed = await original_task
        assert completed is store.current
        assert len(clock.calls) == 1 and store.version == 1

    asyncio.run(scenario())


def test_demonstrations_are_private_and_do_not_advance_background_schedule(setup_store):
    store, clock = setup_store

    async def scenario():
        await start_background(store, clock)
        shared = store.current
        demonstration = await store.demonstrate(replace(store.config.energy_plan, protection_preference=80.))
        assert demonstration.demonstration
        assert demonstration.settings.protection_preference == 80.
        assert store.current is shared and store.version == 1
        clock.latest += timedelta(minutes=5)
        await store.check_for_update()
        await finish(store)
        assert store.current.settings.protection_preference == 50.

    asyncio.run(scenario())


@pytest.mark.parametrize("background_finishes", [False, True])
@pytest.mark.parametrize("has_override", [False, True])
def test_background_supersedes_demo_even_if_demo_finishes_later(setup_store, monkeypatch, background_finishes, has_override):
    store, clock = setup_store
    tab = PlanTab(store.config, str(store.config_path), store)
    tab.energy_plan_override = replace(store.config.energy_plan, protection_preference=80.) if has_override else None
    control = SimpleNamespace(set_visibility=lambda value: None, disable=lambda: None, enable=lambda: None)
    tab.loading = tab.settings_button = tab.status = control
    shown = []
    monkeypatch.setattr(tab, "_apply_run", shown.append)

    async def scenario():
        await store.check_for_update()
        started, release = asyncio.Event(), asyncio.Event()
        demonstrate = store.demonstrate

        async def delayed(settings):
            completed = await demonstrate(settings)
            started.set()
            await release.wait()
            return completed

        monkeypatch.setattr(store, "demonstrate", delayed)
        task = asyncio.create_task(tab.run_demonstration())
        await started.wait()
        if background_finishes:
            clock.latest += timedelta(minutes=1)
            await store.check_for_update()
            await finish(store)
        release.set()
        await task
        assert len(shown) == (0 if background_finishes else 1)
        if shown:
            assert shown[0].demonstration and store.current is None and store.version == 0

    asyncio.run(scenario())


def test_shared_publication_replaces_demo_and_failure_keeps_last_result(setup_store, monkeypatch):
    store, clock = setup_store
    tab = PlanTab(store.config, str(store.config_path), store)
    tab.output = object()
    messages = []
    tab.loading = SimpleNamespace(set_visibility=lambda value: None)
    tab.status = SimpleNamespace(set_visibility=lambda value: None, set_text=messages.append)
    shown = []
    monkeypatch.setattr(tab, "_apply_run", shown.append)
    unsubscribe = store.subscribe(tab.apply_shared_plan)

    async def scenario():
        await start_background(store, clock)
        assert shown == [store.current] and tab.shared_version == store.version
        clock.failure = True
        clock.latest += timedelta(minutes=5)
        await store.check_for_update()
        await finish(store)
        assert len(shown) == 1 and "refresh failed" in messages[-1]
        unsubscribe()

    asyncio.run(scenario())
