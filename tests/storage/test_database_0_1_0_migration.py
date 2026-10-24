"""Legacy migration regression; the current opener owns schema readiness."""
import sqlite3
from solar_model.storage.schema import HOURLY_ROLLUP_MARKER
from solar_model.storage.migrations import open_database


def test_opener_renames_legacy_columns_preserves_values_and_records_readiness(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sigenstor (collected_at_utc INTEGER PRIMARY KEY, inverter_battery_available_discharge_kwh INTEGER)")
        connection.execute("INSERT INTO sigenstor VALUES (3600,194300)")
        connection.execute("CREATE TABLE sigenstor_hourly (hour_start_utc INTEGER, import_tariff_band TEXT, sample_count INTEGER, inverter_battery_available_discharge_kwh INTEGER, PRIMARY KEY(hour_start_utc,import_tariff_band))")
        connection.execute("INSERT INTO sigenstor_hourly VALUES (3600,'Unknown',1,194300)")
    for _ in range(2):
        database = open_database(path)
        try:
            with database.engine.connect() as connection:
                for table in ("sigenstor", "sigenstor_hourly"):
                    columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
                    assert "inverter_battery_available_discharge_kwh" not in columns
                    assert "plant_battery_available_discharge_kwh" in columns
                    assert connection.exec_driver_sql(f"SELECT plant_battery_available_discharge_kwh FROM {table}").scalar_one() == 194300
                assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key=?", (HOURLY_ROLLUP_MARKER,)).scalar_one() == "1"
        finally:
            database.close()
