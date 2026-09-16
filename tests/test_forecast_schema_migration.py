from __future__ import annotations

import sqlite3

from sqlalchemy import inspect

from database import open_database


def test_forecast_schema_renames_legacy_values_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "solar.db"
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE forecast_solar (
            collected_at_utc INTEGER NOT NULL,
            forecast_time INTEGER NOT NULL,
            panel_1_watts INTEGER,
            panel_1_watt_hours INTEGER,
            panel_1_watt_hours_day INTEGER,
            PRIMARY KEY (collected_at_utc, forecast_time)
        )
    """)
    connection.execute("INSERT INTO forecast_solar VALUES (100, 200, 1230000, 4560000, 7890000)")
    connection.commit()
    connection.close()

    database = open_database(path)
    columns = {column["name"] for column in inspect(database.engine).get_columns("forecast_solar")}
    assert "panel_1_watts" not in columns
    assert {"panel_1_raw_watts", "panel_1_adj_watts", "panel_4_adj_watt_hours_day"} <= columns
    with database.engine.connect() as engine_connection:
        raw = engine_connection.exec_driver_sql(
            "SELECT panel_1_raw_watts, panel_1_raw_watt_hours, panel_1_raw_watt_hours_day "
            "FROM forecast_solar"
        ).one()
    assert raw == (1230000, 4560000, 7890000)
    database.close()

    # A second application must be a no-op and retain the original raw values.
    database = open_database(path)
    assert len(database.pending_adjusted_forecasts()) == 1
    snapshots = database.pending_adjusted_forecast_snapshots(limit=1)
    assert snapshots == ["1970-01-01T00:01:40+00:00"]
    database.save_adjusted_forecasts(snapshots[0], {
        "1970-01-01T00:03:20+00:00": {"panel_1_adj_watts": 1.0},
    })
    assert not database.pending_adjusted_forecasts()
    database.close()
