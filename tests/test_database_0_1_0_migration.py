import sqlite3

from migrate_database_0_1_0 import migrate


def test_migration_renames_telemetry_columns_and_sets_database_version(tmp_path) -> None:
    path = tmp_path / "solar.db"
    connection = sqlite3.connect(path)
    try:
        for table in ("sigenstor", "sigenstor_hourly"):
            connection.execute(
                f"CREATE TABLE {table} ("
                "id INTEGER PRIMARY KEY, "
                "inverter_battery_available_discharge_kwh INTEGER"
                ")"
            )
            connection.execute(
                f"INSERT INTO {table} (inverter_battery_available_discharge_kwh) VALUES (194300)"
            )
        connection.commit()
    finally:
        connection.close()

    migrate(path)
    migrate(path)

    migrated = sqlite3.connect(path)
    try:
        for table in ("sigenstor", "sigenstor_hourly"):
            columns = {row[1] for row in migrated.execute(f"PRAGMA table_info({table})")}
            assert "inverter_battery_available_discharge_kwh" not in columns
            assert "plant_battery_available_discharge_kwh" in columns
            assert migrated.execute(
                f"SELECT plant_battery_available_discharge_kwh FROM {table}"
            ).fetchone() == (194300,)
        assert migrated.execute(
            "SELECT value FROM database_metadata WHERE key = 'database_version'"
        ).fetchone() == ("0.1.0",)
    finally:
        migrated.close()
