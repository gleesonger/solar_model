"""Persisted report fidelity, retention boundaries, migration and atomicity."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import cvxpy  # Load native dependencies before UI-related test helpers on Windows.
import pandas as pd
import pytest
from sqlalchemy import event, inspect

from solar_model.config import PlanHistoryConfig, load_config
from solar_model.services.plan_history import payload_frame, persist_report, read_saved_run
from solar_model.services.plan_history import frame_payload
from solar_model.services.planning import PlanReport
from solar_model.storage.migrations import open_database, DATABASE_VERSION
from solar_model.storage.plan_history import list_plans, load_plan, save_plan
from tests.dashboard.test_plan import sample_result


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "solar.db")
    db = open_database(path)
    yield path, db
    db.close()


def test_report_round_trip_keeps_nine_scenarios_precision_and_prepared_views(database):
    path, db = database
    config = load_config()
    config = replace(config, database=replace(config.database, path=path))
    result = sample_result()
    original = result.results["med"].assign(net_cost=[.123456789123, -.234567891234])
    result = replace(result, results={"med" if load == solar == "med" else f"{load}_{solar}": original.copy() for load in ("low", "med", "high") for solar in ("low", "med", "high")})
    stamp = original.starts_at.iloc[0].to_pydatetime()
    actuals = pd.DataFrame({"timestamp": [stamp], "solar": [2.123456789]})
    report = PlanReport(result, actuals, 20., config.energy_plan.scenarios, 3., 50., 20., stamp)
    snapshot_id = persist_report(config, report, stamp, stamp + timedelta(seconds=2))
    saved = read_saved_run(config, snapshot_id)
    assert saved.snapshot_id == snapshot_id
    assert saved.settings == config.energy_plan
    assert saved.full_data_at == stamp
    assert saved.report.simulation.next_action == result.next_action
    pd.testing.assert_frame_equal(saved.report.actuals, actuals)
    for name in result.results:
        pd.testing.assert_frame_equal(saved.report.simulation.results[name], original)
        compact = saved.report.details["Compact"][name]
        assert compact.net_cost.sum() == pytest.approx(original.net_cost.sum(), abs=1e-13)
        assert list(compact.starts_at.dt.minute) == [5, 0]
    assert len(load_plan(path).sections) == 18
    assert inspect(db.engine).has_table("plan_history")


def test_submission_cleanup_keeps_complete_slot_snapshots_and_latest_after_downtime(database):
    path, db = database
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    sections = {("Compact", "med"): {"value": 1}, ("Advanced", "med"): {"value": 2}}
    meta = {"version": 1}
    first = save_plan(path, start, meta, sections, PlanHistoryConfig(30, 30))
    intermediate = save_plan(path, start + timedelta(minutes=5), meta, sections, PlanHistoryConfig(30, 30))
    # Reads do not prune history even if the application has been stopped.
    assert len(list_plans(path)) == 2
    save_plan(path, start + timedelta(minutes=30), meta, sections, PlanHistoryConfig(30, 30))
    assert load_plan(path, intermediate) is None
    next_id = save_plan(path, start + timedelta(minutes=40), meta, sections, PlanHistoryConfig(30, 30))
    assert load_plan(path, first) is not None
    assert load_plan(path, intermediate) is None
    assert load_plan(path, next_id) is not None
    after_day = save_plan(path, start + timedelta(days=1, minutes=1), meta, sections, PlanHistoryConfig(30, 30))
    old = load_plan(path, first)
    assert ("Compact", "med") in old.sections
    assert ("Advanced", "med") in old.sections
    assert ("Advanced", "med") in load_plan(path, after_day).sections
    save_plan(path, start + timedelta(days=31), meta, sections, PlanHistoryConfig(30, 30))
    assert load_plan(path, first) is None
    latest = load_plan(path)
    assert len(list_plans(path)) >= 1
    assert latest is not None
    # No timer or read path deletes even the now-old latest plan.
    assert load_plan(path, latest.snapshot_id) is not None


def test_frequency_changes_rebucket_existing_history_and_period_controls_expiry(database):
    path, db = database
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    sections = {("Compact", "med"): {}, ("Advanced", "med"): {}}
    first = save_plan(path, start, {"version": 1}, sections, PlanHistoryConfig(30, 30))
    middle = save_plan(path, start + timedelta(minutes=30), {"version": 1}, sections, PlanHistoryConfig(30, 30))
    policy = PlanHistoryConfig(retain_snapshots_frequency_mins=60, retain_snapshots_period_days=1)
    latest = save_plan(path, start + timedelta(hours=1), {"version": 1}, sections, policy)
    assert load_plan(path, first) is not None
    assert load_plan(path, middle) is None
    assert ("Advanced", "med") in load_plan(path, latest).sections
    save_plan(path, start + timedelta(days=2), {"version": 1}, sections, policy)
    assert load_plan(path, latest) is None and load_plan(path, first) is None


def test_slot_selection_uses_utc_across_repeated_local_hour(database):
    path, db = database
    stamps = [datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc), datetime(2026, 10, 25, 1, 5, tzinfo=timezone.utc)]
    ids = [save_plan(path, stamp, {"version": 1}, {("Compact", "med"): {}}, PlanHistoryConfig(30, 30)) for stamp in stamps]
    save_plan(path, stamps[-1] + timedelta(hours=1), {"version": 1}, {("Compact", "med"): {}}, PlanHistoryConfig(30, 30))
    assert all(load_plan(path, snapshot_id) is not None for snapshot_id in ids)


def test_failed_retention_rolls_back_new_snapshot_and_preserves_old(database):
    path, db = database
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    original = save_plan(path, start, {"version": 1}, {("Compact", "med"): {}}, PlanHistoryConfig(30, 30))

    def fail_delete(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM plan_history"):
            raise RuntimeError("synthetic cleanup failure")

    event.listen(db.engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="cleanup failure"):
            save_plan(path, start + timedelta(days=31), {"version": 1}, {("Compact", "med"): {}}, PlanHistoryConfig(30, 30))
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_delete)
    assert [item.snapshot_id for item in list_plans(path)] == [original]


def test_upgrade_from_032_does_not_rebuild_existing_telemetry(database, monkeypatch):
    path, db = database
    with db.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE database_metadata SET value='0.3.2' WHERE key='database_version'")
        connection.exec_driver_sql("DROP TABLE plan_history")

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected telemetry rebuild")

    monkeypatch.setattr(type(db), "_insert_hourly_rollup", forbidden)
    reopened = open_database(path)
    try:
        assert inspect(reopened.engine).has_table("plan_history")
        with reopened.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar_one() == DATABASE_VERSION
        assert open_database(path).engine is reopened.engine
    finally:
        reopened.close()


def test_timestamp_codec_preserves_mixed_fractional_and_clock_aligned_intervals():
    frame = pd.DataFrame({"starts_at": pd.to_datetime(["2026-11-10T10:05:12.123456Z", "2026-11-10T10:30:00Z"], format="ISO8601", utc=True)})
    pd.testing.assert_frame_equal(payload_frame(frame_payload(frame)), frame)
