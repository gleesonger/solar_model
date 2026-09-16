"""Leakage-safe calibration of Forecast.Solar snapshots.

This module deliberately owns no schema.  The persistence layer supplies raw
forecast rows and accepts the calculated ``*_adj_*`` values.  Keeping that
boundary small makes the one-off backfill use exactly the same model as the
live collector.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor


SCALE = 10_000  # database.py stores ScaledInteger values at this multiplier
TRAINING_WINDOW_MONTHS = 2
RECENCY_HALF_LIFE_DAYS = 21
PANEL_IDS = (1, 2, 3, 4)
BACKFILL_BATCH_SNAPSHOTS = 24


@dataclass(frozen=True)
class AdjustmentModel:
    model: HistGradientBoostingRegressor
    features: tuple[str, ...]


FEATURES = (
    "raw_solar_kw", "forecast_lead_hours", "hour_sin", "hour_cos",
    "day_sin", "day_cos", "actual_solar_rolling_1h_kwh",
    "actual_solar_rolling_3h_kwh",
)


def _utc(value: datetime | str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _columns(connection, table: str) -> set[str]:
    return set(pd.read_sql_query(f"PRAGMA table_info({table})", connection)["name"])


def _raw_columns(columns: set[str]) -> list[str]:
    return [f"panel_{panel}_raw_watts" for panel in PANEL_IDS if f"panel_{panel}_raw_watts" in columns]


def read_actual_interval_energy(connection, end: pd.Timestamp) -> pd.Series:
    """Read actual inverter interval energy available strictly by ``end``.

    The sample at ``end`` is excluded: a collection cannot observe an interval
    that finishes at precisely the collection timestamp yet.  Missing values
    are preserved rather than invented.
    """
    columns = _columns(connection, "sigenstor")
    energy_columns = [f"inverter_pv{panel}_energy_kwh_period" for panel in PANEL_IDS
                      if f"inverter_pv{panel}_energy_kwh_period" in columns]
    if not energy_columns:
        return pd.Series(dtype=float)
    query = "SELECT collected_at_utc, " + ", ".join(energy_columns) + " FROM sigenstor WHERE collected_at_utc < ? ORDER BY collected_at_utc"
    frame = pd.read_sql_query(query, connection, params=[int(end.timestamp())])
    if frame.empty:
        return pd.Series(dtype=float)
    frame["timestamp"] = pd.to_datetime(frame.pop("collected_at_utc"), unit="s", utc=True)
    # Raw SQL sees ScaledInteger's storage representation.
    values = frame[energy_columns].sum(axis=1, min_count=1) / SCALE
    return pd.Series(values.to_numpy(), index=frame["timestamp"]).sort_index()


def rolling_energy_features(interval_energy: pd.Series, snapshot: pd.Timestamp) -> tuple[float, float]:
    """Return complete rolling kWh windows ending at a snapshot.

    A window is missing unless its recorded interval energy covers the full
    duration.  This avoids silently treating telemetry gaps as zero solar.
    """
    snapshot = _utc(snapshot)
    if interval_energy.empty:
        return np.nan, np.nan
    if not isinstance(interval_energy.index, pd.DatetimeIndex):
        raise ValueError("interval_energy must have a UTC DatetimeIndex")
    result: list[float] = []
    for hours in (1, 3):
        start = snapshot - pd.Timedelta(hours=hours)
        window = interval_energy[(interval_energy.index >= start) & (interval_energy.index < snapshot)]
        # Minute collector data is expected; require at least 90% coverage and
        # a sample near each boundary. This supports a little collection jitter
        # without ever reaching past the snapshot.
        required = max(1, int(hours * 60 * .9))
        coverage = len(window) >= required and window.index.min() <= start + pd.Timedelta(minutes=5) and window.index.max() >= snapshot - pd.Timedelta(minutes=5)
        result.append(float(window.sum()) if coverage else np.nan)
    return result[0], result[1]


def _calendar_features(target: pd.Series) -> pd.DataFrame:
    hour = target.dt.hour + target.dt.minute / 60
    day = target.dt.dayofyear
    return pd.DataFrame({
        "hour_sin": np.sin(2 * np.pi * hour / 24), "hour_cos": np.cos(2 * np.pi * hour / 24),
        "day_sin": np.sin(2 * np.pi * day / 365.25), "day_cos": np.cos(2 * np.pi * day / 365.25),
    }, index=target.index)


def training_frame(connection, as_of: datetime | str | pd.Timestamp,
                   timezone_name: str = "Europe/Dublin") -> pd.DataFrame:
    """Build completed examples using only information observable per snapshot."""
    as_of = _utc(as_of)
    columns = _columns(connection, "forecast_solar")
    raw_columns = _raw_columns(columns)
    if not raw_columns:
        return pd.DataFrame(columns=[*FEATURES, "target_factor", "snapshot"])
    start = int((as_of - pd.DateOffset(months=TRAINING_WINDOW_MONTHS)).timestamp())
    # Live retraining is deliberately frozen at the start of the current local
    # day. This excludes today's incomplete actuals and all current-day target
    # rows, while current actuals remain valid inference features.
    prior_day_cutoff = as_of.tz_convert(timezone_name).normalize().tz_convert("UTC")
    query = "SELECT collected_at_utc, forecast_time, " + ", ".join(raw_columns) + " FROM forecast_solar WHERE collected_at_utc >= ? AND collected_at_utc < ? AND forecast_time < ?"
    forecasts = pd.read_sql_query(query, connection, params=[start, int(as_of.timestamp()), int(prior_day_cutoff.timestamp())])
    if forecasts.empty:
        return pd.DataFrame(columns=[*FEATURES, "target_factor", "snapshot"])
    forecasts["snapshot"] = pd.to_datetime(forecasts.pop("collected_at_utc"), unit="s", utc=True)
    forecasts["target"] = pd.to_datetime(forecasts.pop("forecast_time"), unit="s", utc=True)
    raw_values = forecasts[raw_columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    forecasts["raw_solar_kw"] = raw_values.sum(axis=1) / SCALE / 1000
    # Actual target power is the mean observed power in the target hour.
    actual_columns = _columns(connection, "sigenstor")
    if "plant_pv_power_kw" not in actual_columns:
        return pd.DataFrame(columns=[*FEATURES, "target_factor", "snapshot"])
    actuals = pd.read_sql_query("SELECT collected_at_utc, plant_pv_power_kw FROM sigenstor WHERE collected_at_utc < ?", connection, params=[int(as_of.timestamp())])
    actuals["target"] = pd.to_datetime(actuals.pop("collected_at_utc"), unit="s", utc=True).dt.floor("h")
    actual_hour = actuals.groupby("target")["plant_pv_power_kw"].mean() / SCALE
    forecasts["actual_kw"] = forecasts["target"].map(actual_hour)
    # Cache the as-of feature computation for snapshots shared by many targets.
    energy = read_actual_interval_energy(connection, as_of)
    feature_by_snapshot = {snapshot: rolling_energy_features(energy[energy.index < snapshot], snapshot)
                           for snapshot in forecasts["snapshot"].unique()}
    forecasts[["actual_solar_rolling_1h_kwh", "actual_solar_rolling_3h_kwh"]] = [feature_by_snapshot[x] for x in forecasts["snapshot"]]
    forecasts["forecast_lead_hours"] = (forecasts["target"] - forecasts["snapshot"]).dt.total_seconds() / 3600
    forecasts = forecasts[(forecasts["forecast_lead_hours"] >= 0) & forecasts["actual_kw"].notna() & (forecasts["raw_solar_kw"] > 0)].copy()
    calendar = _calendar_features(forecasts["target"])
    for name in calendar:
        forecasts[name] = calendar[name].to_numpy()
    forecasts["target_factor"] = (forecasts["actual_kw"] / forecasts["raw_solar_kw"]).clip(0, 3)
    return forecasts[[*FEATURES, "target_factor", "snapshot"]]


def fit_model(connection, as_of: datetime | str | pd.Timestamp, minimum_rows: int = 40,
              timezone_name: str = "Europe/Dublin") -> AdjustmentModel | None:
    frame = training_frame(connection, as_of, timezone_name)
    if len(frame) < minimum_rows:
        return None
    age_days = (_utc(as_of) - frame["snapshot"]).dt.total_seconds() / 86_400
    weights = np.exp(-np.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)
    model = HistGradientBoostingRegressor(max_iter=200, max_leaf_nodes=12, l2_regularization=1.0,
                                          learning_rate=.06, random_state=42)
    model.fit(frame[list(FEATURES)], frame["target_factor"], sample_weight=weights)
    return AdjustmentModel(model, FEATURES)


def adjusted_values(raw_rows: Iterable[Mapping[str, object]], adjustment: AdjustmentModel | None,
                    interval_energy: pd.Series, snapshot: datetime | str | pd.Timestamp,
                    timezone_name: str = "Europe/Dublin") -> dict[int, dict[str, float]]:
    """Return per-forecast-time adjusted panel fields from one raw snapshot."""
    snapshot = _utc(snapshot)
    rows = [dict(row) for row in raw_rows]
    if not rows:
        return {}
    feature_1h, feature_3h = rolling_energy_features(interval_energy, snapshot)
    records = []
    for row in rows:
        target = _utc(row["forecast_time"])
        raw_kw = sum(float(row.get(f"panel_{p}_raw_watts") or 0) for p in PANEL_IDS) / 1000
        values = {"raw_solar_kw": raw_kw, "forecast_lead_hours": (target - snapshot).total_seconds() / 3600,
                  "actual_solar_rolling_1h_kwh": feature_1h, "actual_solar_rolling_3h_kwh": feature_3h}
        values.update(_calendar_features(pd.Series([target])).iloc[0].to_dict())
        factor = 1.0 if adjustment is None or raw_kw <= 0 else max(0., float(adjustment.model.predict(pd.DataFrame([values])[list(adjustment.features)])[0]))
        records.append((int(target.timestamp()), target, factor, row))
    # Integrate the adjusted watts forward per configured local date. The final daily
    # value is copied into every row in that day, preserving a coherent path.
    result: dict[int, dict[str, float]] = {}
    timezone = ZoneInfo(timezone_name)
    by_day: dict[date, list[tuple]] = {}
    for record in records:
        by_day.setdefault(record[1].tz_convert(timezone).date(), []).append(record)
    for _, day_rows in by_day.items():
        day_rows.sort(key=lambda item: item[1])
        cumulative = {p: 0.0 for p in PANEL_IDS}
        built: list[tuple[int, dict[str, float]]] = []
        for index, (epoch, target, factor, row) in enumerate(day_rows):
            next_target = day_rows[index + 1][1] if index + 1 < len(day_rows) else target + pd.Timedelta(hours=1)
            hours = max(0., min(6., (next_target - target).total_seconds() / 3600))
            fields: dict[str, float] = {}
            for panel in PANEL_IDS:
                watts = max(0., float(row.get(f"panel_{panel}_raw_watts") or 0) * factor)
                cumulative[panel] += watts * hours
                fields[f"panel_{panel}_adj_watts"] = watts
                fields[f"panel_{panel}_adj_watt_hours"] = cumulative[panel]
            built.append((epoch, fields))
        final = cumulative.copy()
        for epoch, fields in built:
            for panel in PANEL_IDS:
                fields[f"panel_{panel}_adj_watt_hours_day"] = final[panel]
            result[epoch] = fields
    return result


def adjust_pending_forecasts(database, collected_at: datetime | str | pd.Timestamp, timezone_name: str = "Europe/Dublin") -> int:
    """Integration hook for ``SolarDatabase``.

    ``pending_adjusted_forecasts`` must return raw rows for this collection
    snapshot. ``save_adjusted_forecasts`` persists the completed snapshot
    atomically. The persistence migration owns both.
    """
    snapshot = _utc(collected_at)
    pooled_connection = database.engine.raw_connection()
    # Pandas supports sqlite3 connections directly.  SQLAlchemy's pool proxy
    # works too, but emits a warning for every historical snapshot.
    connection = pooled_connection.driver_connection
    try:
        adjustment = fit_model(connection, snapshot, timezone_name=timezone_name)
        energy = read_actual_interval_energy(connection, snapshot)
    finally:
        pooled_connection.close()
    # The database API intentionally returns every unfinished row so the same
    # function also services the migration backfill.  A live call owns only its
    # just-saved snapshot.
    raw_rows = database.pending_adjusted_forecasts(collected_at=snapshot.isoformat())
    values = adjusted_values(raw_rows, adjustment, energy, snapshot, timezone_name)
    database.save_adjusted_forecasts(snapshot.isoformat(), {
        pd.Timestamp(forecast_time, unit="s", tz="UTC").isoformat(): fields
        for forecast_time, fields in values.items()
    })
    return len(values)


def backfill_adjusted_forecasts(database, as_of: datetime | str | pd.Timestamp | None = None,
                                timezone_name: str = "Europe/Dublin") -> int:
    """Populate every unfinished snapshot with a model calibrated at ``as_of``.

    This is intentionally a retrospective convenience path: the single model
    is fitted on recent completed data, while each snapshot's rolling features
    still use only interval energy available at that historical snapshot.
    """
    now = _utc(pd.Timestamp.now(tz="UTC") if as_of is None else as_of)
    pooled_connection = database.engine.raw_connection()
    connection = pooled_connection.driver_connection
    try:
        adjustment = fit_model(connection, now, timezone_name=timezone_name)
        total = 0
        while snapshots := database.pending_adjusted_forecast_snapshots(BACKFILL_BATCH_SNAPSHOTS):
            for stored_snapshot in snapshots:
                snapshot = _utc(stored_snapshot)
                rows = database.pending_adjusted_forecasts(collected_at=stored_snapshot)
                energy = read_actual_interval_energy(connection, snapshot)
                values = adjusted_values(rows, adjustment, energy, snapshot, timezone_name)
                database.save_adjusted_forecasts(snapshot.isoformat(), {
                    pd.Timestamp(forecast_time, unit="s", tz="UTC").isoformat(): fields
                    for forecast_time, fields in values.items()
                })
                total += len(values)
        return total
    finally:
        pooled_connection.close()


def main() -> None:
    """Run the resumable one-off adjusted-forecast backfill."""
    import argparse
    from database import open_database

    parser = argparse.ArgumentParser(description="Backfill adjusted Forecast.Solar rows")
    parser.add_argument("--database", default="solar.db", help="SQLite database path")
    args = parser.parse_args()
    database = open_database(args.database)
    try:
        print(f"Backfilled {backfill_adjusted_forecasts(database)} forecast rows")
    finally:
        database.close()


if __name__ == "__main__":
    main()
