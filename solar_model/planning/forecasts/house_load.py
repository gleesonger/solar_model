"""Daily house-load forecast, excluding separately modelled conversion losses."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo
import numpy as np
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


def history_start_day(periods: list[tuple[str, str]], target_day: date, settings: ForecastModelConfig) -> date:
    """Include 30 baseline days, normal lookback and the latest holiday samples."""
    calendar_start = target_day - timedelta(days=max(30, ceil(settings.lookback)) + 1)
    if not periods:
        return calendar_start
    first_holiday = min(date.fromisoformat(start) for start, _ in periods)
    earlier = sorted((day for day in holiday_days(periods, first_holiday, target_day - timedelta(days=1))), reverse=True)
    count = max(1, ceil(settings.lookback / 2))
    return min(calendar_start, earlier[min(count, len(earlier)) - 1]) if earlier else calendar_start


def predict_day(history: pd.DataFrame, target_day: date, timezone: ZoneInfo, settings: ForecastModelConfig | None = None, holidays: set[date] | None = None) -> pd.DataFrame:
    settings = settings or ForecastModelsConfig().load
    prior_strength = settings.prior_strength
    target_start = pd.Timestamp(datetime.combine(target_day, datetime.min.time(), timezone)).tz_convert("UTC")
    window_start = target_start - pd.Timedelta(days=settings.lookback)
    prior = history[history.timestamp < target_start]
    target_type = "holiday" if target_day in (holidays or ()) else ("weekday" if target_day.weekday() < 5 else "weekend")
    hours = pd.DataFrame({"hour": range(24)})
    if prior.empty:
        return hours.assign(prediction_kwh=np.nan, type_weight=0.0)

    if target_type == "holiday":
        baseline_history = prior[prior.day >= target_day - timedelta(days=30)]
        hourly_baseline = baseline_history.groupby("hour").load_kwh.mean()
        baseline = float(hourly_baseline.min()) if not hourly_baseline.empty else np.nan
        past_holidays = sorted(prior.loc[prior.day_type == "holiday", "day"].unique(), reverse=True)
        selected = past_holidays[:max(1, ceil(settings.lookback / 2))]
        training = prior[prior.day.isin(selected)].copy()
        if training.empty:
            return hours.assign(prediction_kwh=baseline, type_weight=0.0)
        ranks = {day: index + 1 for index, day in enumerate(selected)}
        training["weight"] = np.exp(-np.log(2) * training.day.map(ranks) / (settings.halflife / 2))
        typed = training.groupby("hour").apply(
            lambda rows: np.average(rows.load_kwh, weights=rows.weight), include_groups=False,
        ).rename("type_kwh")
        type_weight = training.groupby("hour").weight.sum().rename("type_weight")
        result = hours.merge(typed, on="hour", how="left").merge(type_weight, on="hour", how="left")
        result["type_weight"] = result.type_weight.fillna(0.0)
        blend = (result.type_weight / (result.type_weight + prior_strength)).fillna(0.0)
        result["prediction_kwh"] = result.type_kwh.fillna(baseline) * blend + baseline * (1 - blend)
        if pd.isna(baseline):
            result["prediction_kwh"] = result.type_kwh
        return result[["hour", "prediction_kwh", "type_weight"]]

    training = prior[(prior.timestamp >= window_start) & (prior.day_type != "holiday")].copy()
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
