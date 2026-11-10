"""Current and historical reports share the same display data contract."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import cvxpy
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
