"""House-load integration, migration recovery and chart consumption."""

from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from solar_model.dashboard.components.charts import energy_chart_options
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.measurements import house_load_energy
from solar_model.storage.migrations import open_database
from solar_model.storage.schema import DatabaseMetadata, SigenStorModbusSample
from solar_model.storage.queries import dashboard as data
from tests.storage.test_grid_power_rollup import tariffs


@pytest.mark.parametrize("old,new,seconds,expected", [
    (1., 3., 60, 1 / 30), (0., 0., 120, 0.),
    (1., 3., 121, None), (1., 3., 0, None),
    (None, 3., 60, None), (1., None, 60, None),
    (float("nan"), 3., 60, None),
])
def test_house_energy(old, new, seconds, expected):
    actual = house_load_energy(old, new, seconds)
    assert actual is None if expected is None else actual == pytest.approx(expected)


def test_collection_restart_missing_readings_and_queries(tmp_path):
    path = tmp_path / "live.db"
    database = open_database(path)
    previous = {}
    for minute, power in [(0, 1.), (1, 3.), (2, None), (3, 3.), (6, 3.), (7, 3.)]:
        values = {} if power is None else {"plant_load_power_kw": (power, "kW", False)}
        stamp = f"2026-10-25T00:{minute:02d}:00+00:00"
        database.save_modbus_sample(values, previous, (stamp, stamp), tariffs())
        previous = database.load_previous_cumulatives()
    with database.sessions() as session:
        samples = session.scalars(select(SigenStorModbusSample).order_by(SigenStorModbusSample.collected_at_utc)).all()
        assert [row.house_load_kwh_period for row in samples] == [None, .0333, None, None, None, .05]
    bounds = data.analysis_date_bounds(date(2026, 10, 25), date(2026, 10, 25), ZoneInfo("UTC"))
    result = data.load_analysis_range(str(path), "UTC", bounds, "day", 1, (), {})
    assert result.energy.house_load.sum() == pytest.approx(.0833)
    custom = data.load_chart_data(str(path), "UTC", bounds, "day", "sum", (), {}, (("house_load", None),))
    assert custom["series"][0]["data"] == pytest.approx([.0833])
    database.close()


def legacy_database(path):
    database = open_database(path)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE database_metadata SET value = '0.1.0' WHERE key = 'database_version'")
        connection.exec_driver_sql("ALTER TABLE sigenstor DROP COLUMN house_load_kwh_period")
        connection.exec_driver_sql("ALTER TABLE sigenstor_hourly DROP COLUMN house_load_kwh_period")
        connection.exec_driver_sql(
            "INSERT INTO sigenstor (collected_at_utc, plant_load_power_kw, import_tariff_band) VALUES "
            "(1792886400, 10000, 'fixed'), (1792886460, 30000, 'fixed'), "
            "(1792886640, 50000, 'fixed')"
        )
    database.close()


def test_old_schema_backfill_and_repeat_open(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    legacy_database(path)
    database = open_database(path)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT house_load_kwh_period FROM sigenstor ORDER BY collected_at_utc").scalars().all() == [None, 333, None]
        assert connection.exec_driver_sql("SELECT SUM(house_load_kwh_period) FROM sigenstor_hourly").scalar() == 333
        assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar() == "0.3.1"
    database.close()
    monkeypatch.setattr(SolarDatabase, "_insert_hourly_rollup", lambda *args: pytest.fail("repeated backfill"))
    open_database(path).close()


def test_failed_backfill_keeps_old_version_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.db"
    legacy_database(path)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError("interrupted")
        patch.setattr(SolarDatabase, "_insert_hourly_rollup", fail)
        with pytest.raises(RuntimeError, match="interrupted"):
            open_database(path)
    from solar_model.storage.connection import create_engine_for_database
    with create_engine_for_database(path).connect() as connection:
        assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar() == "0.1.0"
        assert connection.exec_driver_sql("SELECT COUNT(house_load_kwh_period) FROM sigenstor").scalar() == 0
    database = open_database(path)
    with database.sessions() as session:
        assert session.get(DatabaseMetadata, "database_version").value == "0.3.1"
    database.close()


def test_energy_chart_defaults():
    frame = pd.DataFrame({"period": ["00:00"], "house_load": [.337], "load": [.7],
                          **{key: [0.] for key in ("solar", "forecast_total", "forecast_raw_total", "grid_import", "grid_export")}})
    options = energy_chart_options(frame, "period")
    assert options["legend"]["selected"]["House Load"] is True
    assert options["legend"]["selected"]["Load"] is False
    # Standard charts round display values to one decimal; storage keeps precision.
    assert next(series for series in options["series"] if series["name"] == "House Load")["data"] == [.3]


def test_unknown_version_is_rejected_before_schema_changes(tmp_path):
    path = tmp_path / "future.db"
    legacy_database(path)
    from solar_model.storage.connection import create_engine_for_database
    with create_engine_for_database(path).begin() as connection:
        connection.exec_driver_sql("UPDATE database_metadata SET value='9.0.0' WHERE key='database_version'")
    with pytest.raises(RuntimeError, match="Unsupported database version"):
        open_database(path)
    with create_engine_for_database(path).connect() as connection:
        assert "house_load_kwh_period" not in [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(sigenstor)")]
