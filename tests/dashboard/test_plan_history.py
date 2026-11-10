"""Publication follows persistence; restarts restore the same saved report."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import cvxpy
import pandas as pd
import pytest

from solar_model.config import load_config
from solar_model.dashboard import plan_store
from solar_model.services import plan_history
from solar_model.services.planning import PlanReport
from solar_model.storage.migrations import open_database
from solar_model.storage.plan_history import list_plans, load_plan
from tests.dashboard.test_plan import sample_result


def test_worker_saves_before_publication_restart_restores_and_demos_are_private(tmp_path, monkeypatch):
    config = load_config()
    path = str(tmp_path / "solar.db")
    config = replace(config, database=replace(config.database, path=path))
    database = open_database(path)
    stamp = datetime.now(timezone.utc)
    report = PlanReport(sample_result(), pd.DataFrame(), 20., config.energy_plan.scenarios, 3., 50., 20., stamp)
    monkeypatch.setattr(plan_history, "generate_plan_report", lambda *args: report)
    monkeypatch.setattr(plan_store, "generate_plan_report", lambda *args: report)
    monkeypatch.setattr(plan_store, "latest_full_update", lambda *args: stamp)

    async def warm():
        pass

    async def execute(callback, *args):
        return callback(*args)

    monkeypatch.setattr(plan_store, "ensure_planning_worker", warm)
    monkeypatch.setattr(plan_store.run, "cpu_bound", execute)
    monkeypatch.setattr(plan_store.run, "io_bound", execute)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(open("config.yaml", encoding="utf-8").read(), encoding="utf-8")
    store = plan_store.PlanStore(config, str(config_path))
    notifications = []

    def notified():
        if store.current:
            assert load_plan(path, store.current.snapshot_id) is not None
            notifications.append(store.current.snapshot_id)

    store.subscribe(notified)

    async def scenario():
        completed = await store._refresh(config)
        assert completed is not None and completed.snapshot_id
        assert notifications == [completed.snapshot_id]
        assert completed.report.details is not None
        assert len(list_plans(path)) == 1
        demo = await store.demonstrate(config.energy_plan)
        assert demo.demonstration and demo.snapshot_id is None
        assert len(list_plans(path)) == 1
        # A failed submission keeps the old displayed plan and does not notify a new ID.
        def fail(*args):
            raise RuntimeError("synthetic storage failure")
        monkeypatch.setattr(plan_store, "generate_saved_plan", fail)
        assert await store._refresh(config) is None
        assert store.current is completed
        recovered = plan_store.PlanStore(config, str(config_path))
        await recovered.check_for_update()
        assert recovered.current.snapshot_id == completed.snapshot_id
        assert not recovered.running
        pd.testing.assert_frame_equal(recovered.current.report.simulation.results["med"], report.simulation.results["med"])

    try:
        asyncio.run(scenario())
    finally:
        database.close()
