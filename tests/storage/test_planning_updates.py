"""Committed full-reading markers and the input timestamp retained in reports."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
import sqlite3

import pandas as pd
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from solar_model.config import load_config
from solar_model.services import planning
from solar_model.storage.migrations import open_database
from solar_model.storage.queries.planning import load_latest_full_update
from solar_model.storage.schema import SigenStorModbusSample


def test_planning_checks_reuse_connections_and_see_new_commits(tmp_path):
    path = str(tmp_path / "polling.db")
    database = open_database(path)
    connections = []

    def checkout(connection, record, proxy):
        connections.append(connection)

    event.listen(database.engine, "checkout", checkout)
    stamp = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    try:
        assert planning.latest_full_update(path) is None
        assert database.engine.pool.checkedout() == 0
        with sqlite3.connect(path) as writer:
            writer.execute("INSERT INTO sigenstor (collected_at_utc) VALUES (?)", (int(stamp.timestamp()),))
        assert planning.latest_full_update(path) == stamp
        assert planning.latest_full_update(path) == stamp
        assert len(connections) == 3
        assert all(connection is connections[0] for connection in connections)
        assert database.engine.pool.checkedout() == 0
        with sqlite3.connect(path) as writer:
            writer.execute("DROP TABLE sigenstor")
        with pytest.raises(OperationalError):
            planning.latest_full_update(path)
        assert database.engine.pool.checkedout() == 0
    finally:
        event.remove(database.engine, "checkout", checkout)
        database.close()


def test_report_marker_obeys_the_same_as_of_cutoff_as_battery_state(tmp_path, monkeypatch):
    database = open_database(str(tmp_path / "markers.db"))
    cutoff = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    try:
        assert load_latest_full_update(database, cutoff) is None
        with database.sessions.begin() as session:
            session.add_all([SigenStorModbusSample(collected_at_utc=cutoff + timedelta(minutes=offset), plant_battery_soc_percent=50.) for offset in (-1, 1)])
        assert load_latest_full_update(database, cutoff) == cutoff - timedelta(minutes=1)
        assert load_latest_full_update(database, cutoff + timedelta(minutes=2)) == cutoff + timedelta(minutes=1)
        monkeypatch.setattr(planning, "SolarDatabase", lambda engine: database)
        captured_cutoffs = []

        def calculate(*args, battery_as_of=None):
            captured_cutoffs.append(battery_as_of)
            return object()

        monkeypatch.setattr(planning, "plan_as_of", calculate)
        monkeypatch.setattr(planning, "load_plan_actuals", lambda *args: pd.DataFrame())
        monkeypatch.setattr(planning, "load_device_info_as_of", lambda *args: SimpleNamespace(system_battery_capacity_kwh=18., battery_state_of_health_percent=100.))
        config = load_config()
        config = replace(config, database=replace(config.database, path=str(tmp_path / "markers.db")))
        report = planning.generate_plan_report(config, cutoff)
        assert report.full_data_at == cutoff - timedelta(minutes=1)
        assert captured_cutoffs == [report.full_data_at]
    finally:
        database.close()
