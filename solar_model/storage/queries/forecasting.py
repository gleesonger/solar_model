from __future__ import annotations
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from pathlib import Path
import sqlite3
PANEL_IDS = (1, 2, 3, 4)
SCALE = 10_000


def _load_inputs(database_path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    connection = sqlite3.connect(database_path)
    try:
        fields = ", ".join(f"panel_{panel}_raw_watts" for panel in PANEL_IDS)
        forecasts = pd.read_sql_query(f"SELECT collected_at_utc, forecast_time, {fields} FROM forecast_solar", connection)
        actuals = pd.read_sql_query(
            "SELECT (collected_at_utc / 3600) * 3600 AS target_hour, "
            "AVG(plant_pv_power_kw) / 10000.0 AS plant_pv_power_kw FROM sigenstor "
            "WHERE plant_pv_power_kw IS NOT NULL GROUP BY target_hour ORDER BY target_hour", connection,
        )
    finally:
        connection.close()
    forecasts["snapshot"] = pd.to_datetime(forecasts.pop("collected_at_utc"), unit="s", utc=True)
    forecasts["target"] = pd.to_datetime(forecasts.pop("forecast_time"), unit="s", utc=True)
    forecasts["target_hour"] = forecasts.target.dt.floor("h")
    for panel in PANEL_IDS:
        column = f"panel_{panel}_raw_watts"
        forecasts[f"raw_{panel}_kw"] = pd.to_numeric(forecasts.get(column, 0), errors="coerce").fillna(0) / SCALE / 1_000
    forecasts["raw_total_kw"] = sum(forecasts[f"raw_{panel}_kw"] for panel in PANEL_IDS)
    actuals["target_hour"] = pd.to_datetime(actuals["target_hour"], unit="s", utc=True)
    return forecasts, actuals

def load_operational_inputs(database) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load immutable raw forecast and actual inputs for an operational run."""
    return _load_inputs(database.engine.url.database)

def load_hourly_load(database_path: str | Path, timezone: ZoneInfo, *, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
    connection = sqlite3.connect(database_path)
    try:
        frame = pd.read_sql_query(
            "SELECT hour_start_utc, SUM(plant_load_total_kwh_period) AS load_scaled "
            "FROM sigenstor_hourly WHERE plant_load_total_kwh_period IS NOT NULL "
            "AND hour_start_utc >= :start "
            "AND hour_start_utc < :end "
            "GROUP BY hour_start_utc ORDER BY hour_start_utc",
            connection,
            params={"start": int(start.timestamp()) if start is not None else -(2**63),
                    "end": int(end.timestamp()) if end is not None else 2**63 - 1},
        )
    finally:
        connection.close()
    frame["timestamp"] = pd.to_datetime(frame.pop("hour_start_utc"), unit="s", utc=True)
    frame["local"] = frame.timestamp.dt.tz_convert(timezone)
    frame["day"] = frame.local.dt.date
    frame["hour"] = frame.local.dt.hour
    frame["day_type"] = np.where(frame.local.dt.weekday < 5, "weekday", "weekend")
    frame["load_kwh"] = frame.pop("load_scaled") / SCALE
    return frame[["timestamp", "day", "hour", "day_type", "load_kwh"]]


def load_adjusted_snapshot(database, snapshot: pd.Timestamp) -> pd.DataFrame:
    """Read one saved snapshot in raw scaled units for the energy integration pass."""
    pooled = database.engine.raw_connection()
    try:
        return pd.read_sql_query(
            "SELECT * FROM forecast_solar WHERE collected_at_utc = ?",
            pooled.driver_connection, params=[int(snapshot.timestamp())],
        )
    finally:
        pooled.close()
