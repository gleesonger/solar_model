"""Committed full-reading markers and the input timestamp retained in reports."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd

from solar_model.config import load_config
from solar_model.services import planning
from solar_model.storage.migrations import open_database
from solar_model.storage.queries.planning import load_latest_full_update
from solar_model.storage.schema import SigenStorModbusSample


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
