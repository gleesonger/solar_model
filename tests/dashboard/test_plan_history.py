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
