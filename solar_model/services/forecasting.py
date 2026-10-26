"""Schedule forecast collection, calibrate raw snapshots and freeze daily load."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy.exc import SQLAlchemyError

from solar_model.config import ForecastModelsConfig, SolarArrayConfig
from solar_model.integrations.forecast_solar import fetch_array
from solar_model.logging_setup import LOGGER, heavy_work
from solar_model.planning.forecasts.house_load import predict_day
from solar_model.planning.forecasts.solar import PANEL_IDS, _day_bounds, calculate_adjusted_forecast
from solar_model.storage.queries.forecasting import load_adjusted_snapshot, load_hourly_house_load, load_operational_inputs
from solar_model.time_utils import timestamps, utc_now


def _recalculate_energy(database, snapshot: pd.Timestamp, timezone: ZoneInfo) -> None:
    """Rebuild cumulative/day Wh from the currently stored adjusted watts."""
    frame = load_adjusted_snapshot(database, snapshot)
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

def refresh_daily_house_load_forecast(database, as_of: datetime | str | pd.Timestamp, timezone_name: str = "Europe/Dublin", *, models: ForecastModelsConfig | None = None) -> int:
    """Freeze today's profile once, using history strictly before local midnight."""
    timestamp = pd.Timestamp(as_of)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    zone = ZoneInfo(timezone_name)
    day = timestamp.tz_convert(zone).date()
    if database.has_house_load_forecast(day.isoformat()):
        return 0
    settings = (models or ForecastModelsConfig()).load
    start, end = _day_bounds(day, zone)
    history = load_hourly_house_load(database.engine.url.database, zone,
                               start=start - pd.Timedelta(days=settings.lookback), end=start)
    prediction = predict_day(history, day, zone, settings).set_index("hour").prediction_kwh
    # UTC targets preserve both repeated autumn hours and the missing spring hour.
    targets = pd.date_range(start, end, freq="h", inclusive="left")
    rows = [{"forecast_time": target.isoformat(),
             "load_kwh": float(prediction.loc[target.tz_convert(zone).hour])
                 if pd.notna(prediction.loc[target.tz_convert(zone).hour]) else None}
            for target in targets]
    return database.save_daily_house_load_forecast(day.isoformat(), timestamp.isoformat(), rows)


@heavy_work("solar forecast collection update")
def collect_once(session: requests.Session, database, arrays: tuple[SolarArrayConfig, ...], endpoint: str, timeout_seconds: float, timezone_name: str, interval_seconds: int, models: ForecastModelsConfig | None = None) -> None:
    now = utc_now()
    try:
        due, last_attempt = database.claim_forecast_collection(now, interval_seconds)
    except SQLAlchemyError:
        LOGGER.exception("Unable to check solar forecast schedule; external request skipped")
        return
    if not due:
        zone = ZoneInfo(timezone_name)
        LOGGER.info(
            "Skipping external solar forecast request: last collection attempt %s; next due %s (interval %ss)",
            last_attempt.astimezone(zone).isoformat(timespec="seconds"),
            (last_attempt + timedelta(seconds=interval_seconds)).astimezone(zone).isoformat(timespec="seconds"),
            interval_seconds,
        )
        return
    collected = timestamps(now, timezone_name=timezone_name)
    try:
        count = refresh_daily_house_load_forecast(database, collected[0], timezone_name, models=models)
        LOGGER.info("saved %s daily house-load forecast rows", count)
    except Exception:
        LOGGER.exception("daily house-load forecast failed; will retry at the next scheduled forecast run")
    for array in arrays:
        try:
            count = database.save_forecast(array, fetch_array(session, array, endpoint, timeout_seconds), collected)
            LOGGER.info("saved %s forecast rows for %s", count, array.name)
        except Exception:
            LOGGER.exception("forecast request failed for %s; interval skipped", array.name)
    # Adjust only after every array has contributed to the same wide snapshot;
    # the model learns and corrects the combined site PV output.
    try:
        adjusted = refresh_adjusted_forecast(database, collected[0], timezone_name, models=models)
        LOGGER.info("saved %s adjusted solar forecast rows", adjusted)
    except Exception:
        # Raw API data is the important first-class record. A failed model fit
        # leaves these rows pending for the safe, resumable backfill command.
        LOGGER.exception("solar forecast adjustment failed; raw rows remain pending")
