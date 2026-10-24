"""Solar day-ahead calibration and intraday correction.

Day-ahead learns recency-weighted hourly scalars from historical actuals.
Intraday adjusts future hours using today's completed-hour residuals, shrinking
sparse evidence toward no correction. Raw provider inputs are never changed.
The fixed daily load model is in load.py; calibration settings are in
forecast.model_calibration_parameters.
"""
from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from solar_model.config import ForecastModelConfig, ForecastModelsConfig

PANEL_IDS = (1, 2, 3, 4)


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
