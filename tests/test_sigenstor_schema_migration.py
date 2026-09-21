from sqlalchemy import inspect, text

from database import open_database


def test_available_discharge_column_is_renamed_without_losing_values(tmp_path) -> None:
    path = tmp_path / "solar.db"
    database = open_database(path)
    try:
        with database.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE sigenstor RENAME COLUMN plant_battery_available_discharge_kwh "
                "TO inverter_battery_available_discharge_kwh"
            ))
            connection.execute(text(
                "ALTER TABLE sigenstor_hourly RENAME COLUMN plant_battery_available_discharge_kwh "
                "TO inverter_battery_available_discharge_kwh"
            ))
            connection.execute(text(
                "INSERT INTO sigenstor (collected_at_utc, inverter_battery_available_discharge_kwh) "
                "VALUES (0, 180800)"
            ))
    finally:
        database.engine.dispose()

    migrated = open_database(path)
    try:
        columns = {
            column["name"] for column in inspect(migrated.engine).get_columns("sigenstor")
        }
        hourly_columns = {
            column["name"] for column in inspect(migrated.engine).get_columns("sigenstor_hourly")
        }
        assert "plant_battery_available_discharge_kwh" in columns
        assert "inverter_battery_available_discharge_kwh" not in columns
        assert "plant_battery_available_discharge_kwh" in hourly_columns
        assert "inverter_battery_available_discharge_kwh" not in hourly_columns
        with migrated.engine.connect() as connection:
            value = connection.execute(text(
                "SELECT plant_battery_available_discharge_kwh FROM sigenstor"
            )).scalar_one()
        assert value == 180800
    finally:
        migrated.engine.dispose()
