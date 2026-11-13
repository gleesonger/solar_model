"""Current and historical reports share the same display data contract."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from solar_model.config import load_config
from solar_model.services.plan_history import persist_report, read_saved_run
from solar_model.storage.migrations import open_database
from tests.runtime.helpers import report


def test_historical_snapshot_retains_advanced_summaries_and_chart(tmp_path):
    config = load_config()
    config = replace(config, database=replace(config.database, path=str(tmp_path / "solar.db")))
    db = open_database(config.database.path)
    start = datetime.now(timezone.utc) - timedelta(days=2)
    original = report(config, start)
    try:
        snapshot_id = persist_report(config, original, start, start)
        persist_report(config, report(config, start + timedelta(days=2)), start + timedelta(days=2), start + timedelta(days=2))
        saved = read_saved_run(config, snapshot_id, True)
        assert saved.report.full_resolution
        assert len(saved.report.simulation.results) == 9
        assert saved.report.summaries["med"]["cost"] == pytest.approx(original.simulation.results["med"].net_cost.sum())
        assert saved.report.action_minutes == 15.
        assert saved.report.simulation.results["med"].battery_start_kwh.iloc[0] == 4.
        assert len(saved.report.details["Advanced"]) == 9
    finally:
        db.close()


@pytest.mark.parametrize("switch_selection", [False, True])
def test_actuals_refresh_keeps_forecast_and_ignores_late_selection_results(monkeypatch, switch_selection):
    import asyncio
    from types import SimpleNamespace
    import pandas as pd
    from solar_model.dashboard.tabs import plan
    from tests.dashboard.test_plan import sample_result
    from solar_model.services.planning import PlanReport

    config = load_config()
    tab = plan.PlanTab(config)
    original = PlanReport(sample_result(), pd.DataFrame(), 20.)
    tab.report, tab.result = original, original.simulation
    tab.load_scenario = SimpleNamespace(value="med")
    tab.solar_scenario = SimpleNamespace(value="med")
    calls = []
    tab.chart = SimpleNamespace(options={}, run_chart_method=lambda *args: calls.append(args))
    stamp = original.simulation.results["med"].starts_at.iloc[0] + timedelta(minutes=15)
    actuals = pd.DataFrame({"timestamp": [stamp], "solar": [2.], "house_load": [1.], "battery": [5.], "grid_import": [0.], "grid_export": [.4]})
    async def io(callback, *args):
        if switch_selection:
            tab._history_request += 1
        return actuals
    monkeypatch.setattr(plan.run, "io_bound", io)
    asyncio.run(tab.refresh_actuals())
    assert tab.report is original and tab.result is original.simulation
    assert not tab._actuals_running
    if switch_selection:
        assert not calls and tab.actuals.empty
    else:
        assert tab.actuals is actuals
        assert calls[0][0] == "setOption"
        assert calls[0][2] == {"replaceMerge": ["series"]}


@pytest.mark.parametrize("selected", [None, "pinned"])
@pytest.mark.parametrize("retained", [False, True])
def test_publication_syncs_choices_and_only_replaces_unpinned_report(monkeypatch, selected: str | None, retained: bool) -> None:
    import asyncio
    from types import SimpleNamespace
    from nicegui import ui
    from solar_model.dashboard.tabs import plan

    config = load_config()
    latest = object()
    original = object()
    store = SimpleNamespace(config=config, current=latest, version=2, running=False, last_error=None)
    tab = plan.PlanTab(config, plan_store=store)
    # A connected browser subscribes before its first dashboard snapshot renders.
    asyncio.run(tab.sync_shared_plan())
    assert tab._snapshot_version == -1
    assert tab.shared_version == 0
    tab.output = SimpleNamespace()
    tab.loading = SimpleNamespace(set_visibility=lambda value: None)
    tab.status = SimpleNamespace(set_visibility=lambda value: None)
    tab.report = original
    tab.historical_id = selected
    tab.history_date = SimpleNamespace(value="2026-11-11")
    tab.history_snapshot = ui.select({"pinned": "12:00:00 GMT", "expired": "11:00:00 GMT"}, value=selected)
    applied = []
    def apply(completed) -> None:
        applied.append(completed)
        tab.report = completed
    tab._apply_run = apply
    calls = []
    async def io(callback, *args):
        calls.append(callback)
        plans = [SimpleNamespace(snapshot_id="new", saved_at=datetime(2026, 11, 11, 13, tzinfo=timezone.utc))]
        if retained:
            plans.append(SimpleNamespace(snapshot_id="pinned", saved_at=datetime(2026, 11, 11, 12, tzinfo=timezone.utc)))
        return plans
    monkeypatch.setattr(plan.run, "io_bound", io)
    asyncio.run(tab.sync_shared_plan())
    assert "new" in tab.history_snapshot.options
    assert "expired" not in tab.history_snapshot.options
    assert tab.history_snapshot.value == selected
    assert tab.historical_id == selected
    assert tab.report is (original if selected else latest)
    assert applied == ([] if selected else [latest])
    if selected:
        assert tab.history_snapshot.options[selected] == "12:00:00 GMT"
    asyncio.run(tab.sync_shared_plan())
    assert len(calls) == 1  # Service status changes do not requery the choices.


def test_snapshot_refresh_ignores_old_date_and_preserves_selection_made_during_read(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from nicegui import ui
    from solar_model.dashboard.tabs import plan

    tab = plan.PlanTab(load_config())
    tab.history_date = SimpleNamespace(value="2026-11-11")
    tab.history_snapshot = ui.select({"pinned": "12:00:00 GMT"}, value=None)
    tab.status = SimpleNamespace(set_visibility=lambda value: None)
    async def io(callback, *args):
        tab.history_snapshot.set_value("pinned")
        tab.historical_id = "pinned"
        return [SimpleNamespace(snapshot_id="new", saved_at=datetime(2026, 11, 11, 13, tzinfo=timezone.utc))]
    monkeypatch.setattr(plan.run, "io_bound", io)
    asyncio.run(tab._refresh_snapshot_options())
    assert tab.history_snapshot.value == "pinned"
    assert "new" in tab.history_snapshot.options
    async def stale_io(callback, *args):
        tab._date_request += 1
        tab.history_date.value = "2026-11-12"
        return [SimpleNamespace(snapshot_id="stale", saved_at=datetime(2026, 11, 11, 14, tzinfo=timezone.utc))]
    monkeypatch.setattr(plan.run, "io_bound", stale_io)
    asyncio.run(tab._refresh_snapshot_options())
    assert "stale" not in tab.history_snapshot.options
    assert tab.history_snapshot.value == "pinned"
