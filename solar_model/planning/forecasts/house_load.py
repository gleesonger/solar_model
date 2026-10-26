"""Daily house-load forecast, excluding separately modelled conversion losses."""
from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from solar_model.config import ForecastModelConfig, ForecastModelsConfig


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
