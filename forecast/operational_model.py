"""Production solar correction and fixed daily household-load forecasting.

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

from config import ForecastModelConfig, ForecastModelsConfig


PANEL_IDS = (1, 2, 3, 4)
SCALE = 10_000


def _day_bounds(local_day: date, timezone: ZoneInfo) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(datetime.combine(local_day, datetime.min.time(), timezone))
    return start.tz_convert("UTC"), (start + pd.DateOffset(days=1)).tz_convert("UTC")


def _interpolate(values: np.ndarray, target: pd.Series) -> np.ndarray:
    centres = np.arange(.5, 24, 1)
    return np.interp(target.dt.hour + target.dt.minute / 60, np.r_[centres[-1] - 24, centres, centres[0] + 24], np.r_[values[-1], values, values[0]])


def _fit_scalars(training: pd.DataFrame, as_of: pd.Timestamp, settings: ForecastModelConfig | None = None) -> np.ndarray:
    settings = settings or ForecastModelsConfig().solar_day_ahead
    scalars = np.ones(24)
    if training.empty:
        return scalars
    usable = training[["raw_total_kw", "plant_pv_power_kw", "target"]].dropna()
    usable = usable[usable.target >= as_of - pd.Timedelta(days=settings.lookback)]
    for hour in range(24):
        rows = usable[usable.target.dt.hour == hour]
        if rows.empty:
            continue
        age = (as_of - rows.target).dt.total_seconds().to_numpy() / 86_400
        weights = np.exp(-np.log(2) * np.maximum(age, 0) / settings.halflife)
        raw, actual = rows.raw_total_kw.to_numpy(), rows.plant_pv_power_kw.to_numpy()
        denominator = np.sum(weights * np.square(raw))
        if denominator:
            scalars[hour] = np.sum(weights * raw * actual) / denominator
    return scalars


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


def _hour_distributions(actuals: pd.DataFrame, before: pd.Timestamp, settings: ForecastModelConfig | None = None) -> dict[int, tuple[float, float]]:
    settings = settings or ForecastModelsConfig().solar_day_ahead
    prior = actuals[actuals.target_hour < before]
    result = {}
    for hour, rows in prior.groupby(prior.target_hour.dt.hour):
        if len(rows) < 3:
            continue
        age = (before - rows.target_hour).dt.total_seconds().to_numpy() / 86_400
        weights = np.exp(-np.log(2) * np.maximum(age, 0) / settings.halflife)
        mean = float(np.average(rows.plant_pv_power_kw, weights=weights))
        sigma = float(np.average(np.square(rows.plant_pv_power_kw - mean), weights=weights) ** .5)
        if sigma > 1e-6:
            result[int(hour)] = mean, sigma
    return result


def _today_residual(issued: pd.DataFrame, distributions: dict[int, tuple[float, float]], as_of: pd.Timestamp, settings: ForecastModelConfig | None = None) -> tuple[float, float, int]:
    settings = settings or ForecastModelsConfig().solar_intraday
    prior_strength = settings.prior_strength
    if settings.lookback is not None:
        issued = issued[issued.target_hour >= as_of - pd.Timedelta(hours=settings.lookback)]
    scores, weights = [], []
    for row in issued.itertuples():
        distribution = distributions.get(row.target_hour.hour)
        if distribution is None:
            continue
        _, sigma = distribution
        inputs = (row.raw_total_kw, row.plant_pv_power_kw, row.baseline_kw, sigma)
        if any(value is None or not np.isfinite(value) for value in inputs) or row.raw_total_kw <= 0 or sigma <= 0:
            continue
        score = (row.plant_pv_power_kw - row.baseline_kw) / sigma
        weight = np.exp(-np.log(2) * max((as_of - row.target_hour).total_seconds() / 3_600, 0) / settings.halflife)
        if not np.isfinite(score) or not np.isfinite(weight) or weight <= 0:
            continue
        scores.append(score)
        weights.append(weight)
    if not scores:
        return 0.0, 0.0, 0
    weights = np.asarray(weights)
    effective = float(weights.sum() ** 2 / np.square(weights).sum())
    return float(np.average(scores, weights=weights)), effective / (effective + prior_strength), len(scores)


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


def _training_frames(forecasts: pd.DataFrame, actuals: pd.DataFrame, day, timezone: ZoneInfo, as_of: pd.Timestamp, settings: ForecastModelConfig | None = None) -> pd.DataFrame:
    """Select official vintages only within the scalar lookback, before merging."""
    settings = settings or ForecastModelsConfig().solar_day_ahead
    start, _ = _day_bounds(day, timezone)
    candidates = forecasts[
        (forecasts.target_hour < start)
        & (forecasts.target >= as_of - pd.Timedelta(days=settings.lookback))
    ].copy()
    if candidates.empty:
        return candidates.merge(actuals, on="target_hour", how="left")
    # Select against all collection times, even when the selected snapshot has
    # no targets for this day. Falling back to a different vintage changes training.
    snapshots = pd.DatetimeIndex(forecasts.snapshot.unique()).sort_values()
    local_days = candidates.target_hour.dt.tz_convert(timezone).dt.date
    selected = {}
    for historic_day in sorted(local_days.unique()):
        midnight, next_midnight = _day_bounds(historic_day, timezone)
        position = snapshots.searchsorted(midnight, side="right") - 1
        if position >= 0:
            selected[historic_day] = snapshots[position]
        elif len(snapshots) and snapshots[0] < next_midnight:
            selected[historic_day] = snapshots[0]
    candidates = candidates[candidates.snapshot == local_days.map(selected)]
    return candidates.sort_values("target", kind="stable").merge(actuals, on="target_hour", how="left")


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
    target_day: date | None = None,
    *,
    models: ForecastModelsConfig | None = None,
) -> tuple[pd.Timestamp, dict[str, dict[str, float]]] | None:
    """Calculate one provider snapshot's adjusted values without writing them."""
    models = models or ForecastModelsConfig()
    as_of = pd.Timestamp(as_of)
    as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of.tz_convert("UTC")
    timezone = ZoneInfo(timezone_name)
    day = target_day or as_of.tz_convert(timezone).date()
    official = _official_day_frame(forecasts, actuals, day, timezone)
    if official is None:
        return None
    _, frame = official
    current_available = forecasts[forecasts.snapshot <= as_of]
    if current_available.empty:
        return None
    current_snapshot = current_available.snapshot.max()
    training = _training_frames(forecasts, actuals, day, timezone, as_of, models.solar_day_ahead)
    scalars = _fit_scalars(training, as_of, models.solar_day_ahead)
    day_start, day_end = _day_bounds(day, timezone)
    distributions = _hour_distributions(actuals, day_start, models.solar_day_ahead)
    cutoff = as_of.floor("h")
    past = frame[(frame.target_hour >= day_start) & (frame.target_hour < cutoff)].copy()
    past["baseline_kw"] = _interpolate(scalars, past.target) * past.raw_total_kw
    if as_of < day_start:
        # No observations exist for a future target day.  Do not let an
        # intraday residual alter its calibrated raw forecast.
        residual, strength = 0.0, 0.0
    else:
        residual, strength, _ = _today_residual(past, distributions, as_of, models.solar_intraday)
    first_target = max(cutoff, day_start)
    source_future = frame[frame.target_hour >= first_target]
    source_values = _prediction_fields(source_future, scalars, distributions, residual, strength)
    current_rows = forecasts[
        (forecasts.snapshot == current_snapshot)
        & (forecasts.target_hour >= first_target)
        & (forecasts.target_hour < day_end)
    ]
    # Watts are instantaneous values: preserve exact targets and linearly
    # interpolate differing grids within known coverage, without extrapolation.
    epochs = sorted(source_values)
    values = {}
    if epochs:
        fields = tuple(source_values[epochs[0]])
        for target in current_rows.target:
            epoch = int(target.timestamp())
            if epochs[0] <= epoch <= epochs[-1]:
                values[target.isoformat()] = {
                    field: float(np.interp(epoch, epochs, [source_values[t][field] for t in epochs]))
                    for field in fields
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


def refresh_adjusted_forecast(database, as_of: datetime | str | pd.Timestamp, timezone_name: str = "Europe/Dublin", *, models: ForecastModelsConfig | None = None) -> int:
    """Write a fresh adjusted forecast to the current raw-collection snapshot.

    The calculation always starts from the official pre-midnight raw snapshot,
    but every collection retains its own adjusted output for auditability.
    """
    forecasts, actuals = load_operational_inputs(database)
    timestamp = pd.Timestamp(as_of)
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    available = forecasts[forecasts.snapshot <= timestamp]
    if available.empty:
        return 0
    current_snapshot = available.snapshot.max()
    timezone = ZoneInfo(timezone_name)
    rows = forecasts[forecasts.snapshot == current_snapshot]
    values: dict[str, dict[str, float]] = {}
    for target_day in sorted(rows.target_hour.dt.tz_convert(timezone).dt.date.unique()):
        calculated = calculate_adjusted_forecast(
            forecasts, actuals, timestamp, timezone_name, target_day=target_day, models=models,
        )
        if calculated is not None:
            _, target_values = calculated
            values.update(target_values)
    if not values:
        return 0
    database.save_adjusted_forecasts(current_snapshot.isoformat(), values)
    _recalculate_energy(database, current_snapshot, timezone)
    return len(values)


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


def predict_day(history: pd.DataFrame, target_day: date, timezone: ZoneInfo, settings: ForecastModelConfig | None = None) -> pd.DataFrame:
    settings = settings or ForecastModelsConfig().load
    prior_strength = settings.prior_strength
    target_start = pd.Timestamp(datetime.combine(target_day, datetime.min.time(), timezone)).tz_convert("UTC")
    window_start = target_start - pd.Timedelta(days=settings.lookback)
    training = history[(history.timestamp < target_start) & (history.timestamp >= window_start)].copy()
    target_type = "weekday" if target_day.weekday() < 5 else "weekend"
    hours = pd.DataFrame({"hour": range(24)})
    if training.empty:
        return hours.assign(prediction_kwh=np.nan, type_weight=0.0)
    age = (target_start - training.timestamp).dt.total_seconds() / 86_400
    training["weight"] = np.exp(-np.log(2) * age / settings.halflife)
    all_days = training.groupby("hour").apply(
        lambda rows: np.average(rows.load_kwh, weights=rows.weight), include_groups=False,
    ).rename("all_days_kwh")
    typed_training = training[training.day_type == target_type]
    typed = typed_training.groupby("hour").apply(
        lambda rows: np.average(rows.load_kwh, weights=rows.weight), include_groups=False,
    ).rename("type_kwh") if not typed_training.empty else pd.Series(
        name="type_kwh", index=pd.Index([], name="hour"), dtype=float,
    )
    type_weight = training[training.day_type == target_type].groupby("hour").weight.sum().rename("type_weight")
    result = hours.merge(all_days, on="hour", how="left").merge(typed, on="hour", how="left").merge(type_weight, on="hour", how="left")
    result["type_weight"] = result.type_weight.fillna(0.0)
    # 20% after one unit of evidence gives the original prior strength of 4.
    # Keep fractional strengths when tuning the decimal influence; do not round.
    # With 100% influence, missing type evidence still uses the all-days mean.
    blend = (result.type_weight / (result.type_weight + prior_strength)).fillna(0.0)
    result["prediction_kwh"] = result.type_kwh.fillna(result.all_days_kwh) * blend + result.all_days_kwh * (1 - blend)
    return result[["hour", "prediction_kwh", "type_weight"]]


def refresh_daily_load_forecast(database, as_of: datetime | str | pd.Timestamp, timezone_name: str = "Europe/Dublin", *, models: ForecastModelsConfig | None = None) -> int:
    """Freeze today's profile once, using history strictly before local midnight."""
    timestamp = pd.Timestamp(as_of)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    zone = ZoneInfo(timezone_name)
    day = timestamp.tz_convert(zone).date()
    if database.has_load_forecast(day.isoformat()):
        return 0
    settings = (models or ForecastModelsConfig()).load
    start, end = _day_bounds(day, zone)
    history = load_hourly_load(database.engine.url.database, zone,
                               start=start - pd.Timedelta(days=settings.lookback), end=start)
    prediction = predict_day(history, day, zone, settings).set_index("hour").prediction_kwh
    # UTC targets preserve both repeated autumn hours and the missing spring hour.
    targets = pd.date_range(start, end, freq="h", inclusive="left")
    rows = [{"forecast_time": target.isoformat(),
             "load_kwh": float(prediction.loc[target.tz_convert(zone).hour])
                 if pd.notna(prediction.loc[target.tz_convert(zone).hour]) else None}
            for target in targets]
    return database.save_daily_load_forecast(day.isoformat(), timestamp.isoformat(), rows)
