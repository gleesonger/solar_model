"""Holiday table migration from the existing database version."""
import pytest
from sqlalchemy import inspect

from solar_model.storage.migrations import open_database
from solar_model.storage.schema import HolidayPeriod


@pytest.mark.parametrize("existing_holidays", [False, True])
def test_open_database_migrates_existing_version_and_preserves_forecasts(tmp_path, existing_holidays):
    path = tmp_path / "existing.db"
    database = open_database(path)
    try:
        database.save_daily_house_load_forecast("2026-11-05", "2026-11-05T00:00Z", [
            {"forecast_time": "2026-11-05T00:00Z", "load_kwh": 1.25},
        ])
        if existing_holidays:
            database.replace_holiday_periods([("2026-12-24", "2026-12-25")])
        with database.engine.begin() as connection:
            if not existing_holidays:
                HolidayPeriod.__table__.drop(connection)
            connection.exec_driver_sql("UPDATE database_metadata SET value='0.3.1' WHERE key='database_version'")
    finally:
        database.close()

    migrated = open_database(path)
    try:
        assert inspect(migrated.engine).has_table("holiday_period")
        with migrated.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar() == "0.3.3"
            assert connection.exec_driver_sql("SELECT load_kwh FROM forecast_house_load").scalar() == 12500
        if existing_holidays:
            assert migrated.list_holiday_periods() == [("2026-12-24", "2026-12-25")]
        migrated.replace_holiday_periods([("2026-12-24", "2026-12-25")])
    finally:
        migrated.close()

    reopened = open_database(path)
    try:
        assert reopened.list_holiday_periods() == [("2026-12-24", "2026-12-25")]
    finally:
        reopened.close()
