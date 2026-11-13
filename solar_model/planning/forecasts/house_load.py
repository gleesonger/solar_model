"""Daily house-load forecast, excluding separately modelled conversion losses."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil, isfinite, nan
from zoneinfo import ZoneInfo
import pandas as pd
from solar_model.config import ForecastModelConfig, ForecastModelsConfig


def holiday_days(periods: list[tuple[str, str]], start: date, end: date) -> set[date]:
    """Expand inclusive ranges only within the dates needed by a forecast."""
    days = set()
    for first, last in periods:
        current = max(start, date.fromisoformat(first))
        stop = min(end, date.fromisoformat(last))
        while current <= stop:
            days.add(current)
            current += timedelta(days=1)
    return days


@dataclass(frozen=True)
class LoadObservation:
    timestamp: pd.Timestamp
    load_kwh: float


def normal_history_start_day(target_day: date, lookback: float, holidays: set[date]) -> date:
    """Count complete local dates, skipping holidays but not missing readings."""
    remaining = ceil(lookback)
    current = target_day
    while remaining:
        current -= timedelta(days=1)
        if current not in holidays:
            remaining -= 1
    return current


def history_start_day(target_day: date, settings: ForecastModelConfig, periods: list[tuple[str, str]]) -> date:
    """Read enough history for the normal window and calendar-based holiday fit."""
    first_holiday = min((date.fromisoformat(start) for start, _ in periods), default=target_day)
    holidays = holiday_days(periods, first_holiday, target_day - timedelta(days=1))
    return normal_history_start_day(target_day, settings.lookback, holidays) - timedelta(days=1)


def blend_observations(
    layers: list[list[LoadObservation]],
    target_start: pd.Timestamp,
    settings: ForecastModelConfig,
    *,
    window_start: pd.Timestamp | None = None,
) -> tuple[float, list[float]]:
    """Blend broad-to-specific timestamped observations with one set of settings."""
    prediction = nan
    evidence = []
    window_start = window_start if window_start is not None else target_start - pd.Timedelta(days=settings.lookback)
    prior_strength = settings.prior_strength
    for observations in layers:
        total_weight, weighted_load = 0., 0.
        for observation in observations:
            if not window_start <= observation.timestamp < target_start or not isfinite(observation.load_kwh):
                continue
            age = (target_start - observation.timestamp).total_seconds() / 86_400
            weight = 2 ** (-age / settings.halflife)
            total_weight += weight
            weighted_load += weight * observation.load_kwh
        evidence.append(total_weight)
        if total_weight == 0:
            continue
        mean = weighted_load / total_weight
        if not isfinite(prediction):
            prediction = mean
        else:
            influence = total_weight / (total_weight + prior_strength)
            prediction = (1 - influence) * prediction + influence * mean
    return prediction, evidence


def predict_day(history: pd.DataFrame, target_day: date, timezone: ZoneInfo, settings: ForecastModelConfig | None = None, holidays: set[date] | None = None) -> pd.DataFrame:
    """Use the same three-layer hourly model for holiday and normal history."""
    settings = settings or ForecastModelsConfig().load
    target_start = pd.Timestamp(datetime.combine(target_day, datetime.min.time(), timezone)).tz_convert("UTC")
    window_start = target_start - pd.Timedelta(days=settings.lookback)
    prior = history[history.timestamp < target_start]
    known_holidays = set(holidays or ()) | set(prior.loc[prior.day_type == "holiday", "day"])
    normal_start_day = normal_history_start_day(target_day, settings.lookback, known_holidays)
    normal_start = pd.Timestamp(datetime.combine(normal_start_day, datetime.min.time(), timezone)).tz_convert("UTC")
    # Keep separate streams; holiday rows still have their actual day of week.
    streams: dict[bool, list[list[list[LoadObservation]]]] = {
        False: [[[], [], []] for _ in range(24)],
        True: [[[], [], []] for _ in range(24)],
    }
    target_weekend = target_day.weekday() >= 5
    for row in prior.itertuples():
        if pd.isna(row.load_kwh) or not isfinite(float(row.load_kwh)):
            continue
        is_holiday = row.day_type == "holiday" or row.day in (holidays or ())
        if row.timestamp < (window_start if is_holiday else normal_start):
            continue
        layers = streams[is_holiday][row.hour]
        observation = LoadObservation(row.timestamp, float(row.load_kwh))
        layers[0].append(observation)
        if (row.day.weekday() >= 5) == target_weekend:
            layers[1].append(observation)
        if row.day.weekday() == target_day.weekday():
            layers[2].append(observation)
    target_holiday = target_day in (holidays or ())
    records = []
    for hour in range(24):
        layers = streams[target_holiday][hour]
        prediction, evidence = blend_observations(layers, target_start, settings, window_start=window_start if target_holiday else normal_start)
        if target_holiday and not isfinite(prediction):
            prediction, evidence = blend_observations(streams[False][hour], target_start, settings, window_start=normal_start)
        records.append({"hour": hour, "prediction_kwh": prediction, "type_weight": evidence[1]})
    return pd.DataFrame(records)
