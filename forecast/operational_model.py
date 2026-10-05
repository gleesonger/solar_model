"""Production start-of-day scalar forecast with intraday normal residuals.

Raw Forecast.Solar snapshots are immutable.  The adjusted fields of the one
official snapshot for a local day (the final snapshot before midnight) are
updated as actual PV arrives.  A target hour is updated only while it is still
future, so a stored adjusted prediction never incorporates that hour's actual.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


PANEL_IDS = (1, 2, 3, 4)
SCALE = 10_000
SCALAR_HALF_LIFE_DAYS = 21
SCALAR_LOOKBACK_DAYS = 56
RESIDUAL_HALF_LIFE_HOURS = 1.0
RESIDUAL_STRENGTH_HOURS = 2.0


def _day_bounds(local_day: date, timezone: ZoneInfo) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(datetime.combine(local_day, datetime.min.time(), timezone))
    return start.tz_convert("UTC"), (start + pd.DateOffset(days=1)).tz_convert("UTC")


def _interpolate(values: np.ndarray, target: pd.Series) -> np.ndarray:
    centres = np.arange(.5, 24, 1)
    return np.interp(target.dt.hour + target.dt.minute / 60, np.r_[centres[-1] - 24, centres, centres[0] + 24], np.r_[values[-1], values, values[0]])


def _fit_scalars(training: pd.DataFrame, as_of: pd.Timestamp) -> np.ndarray:
    scalars = np.ones(24)
    if training.empty:
        return scalars
    usable = training[["raw_total_kw", "plant_pv_power_kw", "target"]].dropna()
    usable = usable[usable.target >= as_of - pd.Timedelta(days=SCALAR_LOOKBACK_DAYS)]
    for hour in range(24):
        rows = usable[usable.target.dt.hour == hour]
        if rows.empty:
            continue
        age = (as_of - rows.target).dt.total_seconds().to_numpy() / 86_400
        weights = np.exp(-np.log(2) * np.maximum(age, 0) / SCALAR_HALF_LIFE_DAYS)
        raw, actual = rows.raw_total_kw.to_numpy(), rows.plant_pv_power_kw.to_numpy()
        denominator = np.sum(weights * np.square(raw))
        if denominator:
            scalars[hour] = np.sum(weights * raw * actual) / denominator
    return scalars


def _load_inputs(database_path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    connection = sqlite3.connect(database_path)
    try:
        forecasts = pd.read_sql_query("SELECT * FROM forecast_solar", connection)
        actuals = pd.read_sql_query("SELECT collected_at_utc, plant_pv_power_kw FROM sigenstor WHERE plant_pv_power_kw IS NOT NULL", connection)
    finally:
        connection.close()
    forecasts["snapshot"] = pd.to_datetime(forecasts.pop("collected_at_utc"), unit="s", utc=True)
    forecasts["target"] = pd.to_datetime(forecasts.pop("forecast_time"), unit="s", utc=True)
    forecasts["target_hour"] = forecasts.target.dt.floor("h")
    for panel in PANEL_IDS:
        column = f"panel_{panel}_raw_watts"
        forecasts[f"raw_{panel}_kw"] = pd.to_numeric(forecasts.get(column, 0), errors="coerce").fillna(0) / SCALE / 1_000
    forecasts["raw_total_kw"] = sum(forecasts[f"raw_{panel}_kw"] for panel in PANEL_IDS)
    actuals["target_hour"] = pd.to_datetime(actuals.pop("collected_at_utc"), unit="s", utc=True).dt.floor("h")
    actuals = actuals.groupby("target_hour", as_index=False).mean(numeric_only=True)
    actuals["plant_pv_power_kw"] /= SCALE
    return forecasts, actuals


def _hour_distributions(actuals: pd.DataFrame, before: pd.Timestamp) -> dict[int, tuple[float, float]]:
    prior = actuals[actuals.target_hour < before]
    result = {}
    for hour, rows in prior.groupby(prior.target_hour.dt.hour):
        if len(rows) < 3:
            continue
        age = (before - rows.target_hour).dt.total_seconds().to_numpy() / 86_400
        weights = np.exp(-np.log(2) * np.maximum(age, 0) / SCALAR_HALF_LIFE_DAYS)
        mean = float(np.average(rows.plant_pv_power_kw, weights=weights))
        sigma = float(np.average(np.square(rows.plant_pv_power_kw - mean), weights=weights) ** .5)
        if sigma > 1e-6:
            result[int(hour)] = mean, sigma
    return result


def _today_residual(issued: pd.DataFrame, distributions: dict[int, tuple[float, float]], as_of: pd.Timestamp) -> tuple[float, float, int]:
    scores, weights = [], []
    for row in issued.itertuples():
        distribution = distributions.get(row.target_hour.hour)
        if distribution is None or row.raw_total_kw <= 0:
            continue
        mean, sigma = distribution
        scores.append((row.plant_pv_power_kw - row.baseline_kw) / sigma)
        weights.append(np.exp(-np.log(2) * max((as_of - row.target_hour).total_seconds() / 3_600, 0) / RESIDUAL_HALF_LIFE_HOURS))
    if not scores:
        return 0.0, 0.0, 0
    weights = np.asarray(weights)
    effective = float(weights.sum() ** 2 / np.square(weights).sum())
    return float(np.average(scores, weights=weights)), effective / (effective + RESIDUAL_STRENGTH_HOURS), len(scores)


def _official_day_frame(forecasts: pd.DataFrame, actuals: pd.DataFrame, day, timezone: ZoneInfo) -> tuple[pd.Timestamp, pd.DataFrame] | None:
    start, end = _day_bounds(day, timezone)
    available = forecasts[forecasts.snapshot <= start]
    if available.empty:
        # A new installation can begin after midnight; use its first snapshot
        # for that day until the next normal start-of-day snapshot exists.
        available = forecasts[(forecasts.snapshot >= start) & (forecasts.snapshot < end)]
        if available.empty:
            return None
        snapshot = available.snapshot.min()
    else:
        snapshot = available.snapshot.max()
    frame = available[(available.snapshot == snapshot) & (available.target_hour >= start) & (available.target_hour < end)]
    frame = frame.merge(actuals, on="target_hour", how="left")
    return snapshot, frame


def _training_frames(forecasts: pd.DataFrame, actuals: pd.DataFrame, day, timezone: ZoneInfo) -> pd.DataFrame:
    first = forecasts.target_hour.dt.tz_convert(timezone).dt.date.min()
    frames = []
    for historic_day in pd.date_range(first, day, freq="D").date:
        if historic_day >= day:
            break
        official = _official_day_frame(forecasts, actuals, historic_day, timezone)
        if official is not None:
            frames.append(official[1])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _prediction_fields(frame: pd.DataFrame, scalars, distributions, residual: float, strength: float) -> dict[int, dict[str, float]]:
    values: dict[int, dict[str, float]] = {}
    for row in frame.itertuples():
        raw_watts = {panel: float(getattr(row, f"raw_{panel}_kw", 0) or 0) * 1_000 for panel in PANEL_IDS}
        raw_total = sum(raw_watts.values())
        scalar = float(_interpolate(scalars, pd.Series([row.target]))[0])
        baseline = scalar * raw_total / 1_000
        distribution = distributions.get(row.target_hour.hour)
        correction = 0.0 if distribution is None or raw_total <= 0 else distribution[1] * strength * residual
        fields: dict[str, float] = {}
        for panel in PANEL_IDS:
            share = raw_watts[panel] / raw_total if raw_total else 0.0
            fields[f"panel_{panel}_adj_watts"] = scalar * raw_watts[panel] + correction * 1_000 * share
        values[int(row.target.timestamp())] = fields
    return values


def load_operational_inputs(database) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load immutable raw forecast and actual inputs for an operational run."""
    return _load_inputs(database.engine.url.database)


def calculate_adjusted_forecast(
    forecasts: pd.DataFrame,
    actuals: pd.DataFrame,
    as_of: datetime | str | pd.Timestamp,
    timezone_name: str = "Europe/Dublin",
) -> tuple[pd.Timestamp, dict[str, dict[str, float]]] | None:
    """Calculate one provider snapshot's adjusted values without writing them."""
    as_of = pd.Timestamp(as_of)
    as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of.tz_convert("UTC")
    timezone = ZoneInfo(timezone_name)
    day = as_of.tz_convert(timezone).date()
    official = _official_day_frame(forecasts, actuals, day, timezone)
    if official is None:
        return None
    _, frame = official
    current_available = forecasts[forecasts.snapshot <= as_of]
    if current_available.empty:
        return None
    current_snapshot = current_available.snapshot.max()
    training = _training_frames(forecasts, actuals, day, timezone)
    scalars = _fit_scalars(training, as_of)
    day_start, day_end = _day_bounds(day, timezone)
    distributions = _hour_distributions(actuals, day_start)
    cutoff = as_of.floor("h")
    past = frame[(frame.target_hour >= day_start) & (frame.target_hour < cutoff)].copy()
    past["baseline_kw"] = _interpolate(scalars, past.target) * past.raw_total_kw
    residual, strength, _ = _today_residual(past, distributions, as_of)
    source_future = frame[frame.target_hour >= cutoff]
    source_values = _prediction_fields(source_future, scalars, distributions, residual, strength)
    fields_by_hour = {
        row.target_hour: source_values[int(row.target.timestamp())]
        for row in source_future.itertuples()
    }
    current_rows = forecasts[
        (forecasts.snapshot == current_snapshot)
        & (forecasts.target_hour >= cutoff)
        & (forecasts.target_hour < day_end)
    ]
    values = {
        row.target.isoformat(): fields_by_hour[row.target_hour]
        for row in current_rows.itertuples()
        if row.target_hour in fields_by_hour
    }
    return current_snapshot, values


def _recalculate_energy(database, snapshot: pd.Timestamp, timezone: ZoneInfo) -> None:
    """Rebuild cumulative/day Wh from the currently stored adjusted watts."""
    pooled = database.engine.raw_connection()
    raw = pooled.driver_connection
    try:
        frame = pd.read_sql_query("SELECT * FROM forecast_solar WHERE collected_at_utc = ?", raw, params=[int(snapshot.timestamp())])
    finally:
        pooled.close()
    if frame.empty:
        return
    frame["target"] = pd.to_datetime(frame.forecast_time, unit="s", utc=True)
    frame["day"] = frame.target.dt.tz_convert(timezone).dt.date
    output: dict[str, dict[str, float]] = {}
    for _, group in frame.groupby("day"):
        group = group.sort_values("target")
        totals = {panel: 0.0 for panel in PANEL_IDS}
        built = []
        for index, row in enumerate(group.itertuples()):
            next_target = group.iloc[index + 1].target if index + 1 < len(group) else row.target + pd.Timedelta(hours=1)
            hours = max(0.0, min(6.0, (next_target - row.target).total_seconds() / 3_600))
            fields = {}
            for panel in PANEL_IDS:
                watts = getattr(row, f"panel_{panel}_adj_watts")
                watts = 0.0 if watts is None or pd.isna(watts) else float(watts) / 10_000
                totals[panel] += watts * hours
                fields[f"panel_{panel}_adj_watt_hours"] = totals[panel]
            built.append((row.target, fields))
        for target, fields in built:
            for panel in PANEL_IDS:
                fields[f"panel_{panel}_adj_watt_hours_day"] = totals[panel]
            output[target.isoformat()] = fields
    database.save_adjusted_forecasts(snapshot.isoformat(), output)


def add_adjusted_energy_values(
    forecasts: pd.DataFrame,
    snapshot: pd.Timestamp,
    local_day,
    timezone: ZoneInfo,
    watt_updates: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Add cumulative/day Wh fields to one rebuilt snapshot in memory."""
    day_start, day_end = _day_bounds(local_day, timezone)
    frame = forecasts[
        (forecasts.snapshot == snapshot)
        & (forecasts.target_hour >= day_start)
        & (forecasts.target_hour < day_end)
    ].copy().sort_values("target")
    output = {target: dict(values) for target, values in watt_updates.items()}
    totals = {panel: 0.0 for panel in PANEL_IDS}
    for index, row in enumerate(frame.itertuples()):
        target = row.target.isoformat()
        fields = output.setdefault(target, {})
        next_target = frame.iloc[index + 1].target if index + 1 < len(frame) else row.target + pd.Timedelta(hours=1)
        hours = max(0.0, min(6.0, (next_target - row.target).total_seconds() / 3_600))
        for panel in PANEL_IDS:
            watts = fields.get(f"panel_{panel}_adj_watts")
            # These updates are model outputs in physical watts.  Unlike the
            # raw-SQL path in _recalculate_energy, they have not been through
            # the database's ScaledInteger storage representation.
            watts = 0.0 if watts is None or pd.isna(watts) else float(watts)
            totals[panel] += watts * hours
            fields[f"panel_{panel}_adj_watt_hours"] = totals[panel]
    for fields in output.values():
        for panel in PANEL_IDS:
            fields[f"panel_{panel}_adj_watt_hours_day"] = totals[panel]
    return output


def refresh_adjusted_forecast(database, as_of: datetime | str | pd.Timestamp, timezone_name: str = "Europe/Dublin") -> int:
    """Write a fresh adjusted forecast to the current raw-collection snapshot.

    The calculation always starts from the official pre-midnight raw snapshot,
    but every collection retains its own adjusted output for auditability.
    """
    forecasts, actuals = load_operational_inputs(database)
    calculated = calculate_adjusted_forecast(
        forecasts, actuals, as_of, timezone_name,
    )
    if calculated is None:
        return 0
    current_snapshot, values = calculated
    database.save_adjusted_forecasts(current_snapshot.isoformat(), values)
    _recalculate_energy(database, current_snapshot, ZoneInfo(timezone_name))
    return len(values)
