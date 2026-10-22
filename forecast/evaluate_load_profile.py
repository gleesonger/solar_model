"""Backtest a recency-weighted hourly household-load profile.

This is intentionally a report-only experiment.  It uses a six-week lookback
and a two-week half-life.  The local weekday/weekend hourly estimate is
shrunk towards the all-days hourly estimate, so scarce weekend observations
benefit from weekday history without being treated as identical to weekdays.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


LOOKBACK_DAYS = 42
HALF_LIFE_DAYS = 14
PRIOR_EFFECTIVE_OBSERVATIONS = 4.0
SCALE = 10_000


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the hierarchical hourly load profile")
    parser.add_argument("--database", default="solar.db")
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--timezone", default="Europe/Dublin")
    args = parser.parse_args()
    timezone = ZoneInfo(args.timezone)
    result = evaluate(load_hourly_load(args.database, timezone), args.start, args.end, timezone)
    if result.empty:
        print("No load observations available for the requested period.")
        return
    result["absolute_error_kwh"] = (result.prediction_kwh - result.load_kwh).abs()
    result["error_kwh"] = result.prediction_kwh - result.load_kwh
    summary = result.groupby("day_type").agg(
        samples=("hour", "size"), mae_kwh=("absolute_error_kwh", "mean"), bias_kwh=("error_kwh", "mean"),
    )
    total = pd.DataFrame({"samples": [len(result)], "mae_kwh": [result.absolute_error_kwh.mean()], "bias_kwh": [result.error_kwh.mean()]}, index=["total"])
    print(pd.concat([summary, total]).to_string(float_format=lambda value: f"{value:.3f}"))
    print("\nDaily total comparison (kWh):")
    print(result.groupby("day").agg(actual=("load_kwh", "sum"), forecast=("prediction_kwh", "sum")).to_string(float_format=lambda value: f"{value:.2f}"))


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def load_hourly_load(database_path: str | Path, timezone: ZoneInfo) -> pd.DataFrame:
    connection = sqlite3.connect(database_path)
    try:
        frame = pd.read_sql_query(
            "SELECT hour_start_utc, SUM(plant_load_total_kwh_period) AS load_scaled "
            "FROM sigenstor_hourly WHERE plant_load_total_kwh_period IS NOT NULL "
            "GROUP BY hour_start_utc ORDER BY hour_start_utc",
            connection,
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


def evaluate(history: pd.DataFrame, start: date, end: date, timezone: ZoneInfo) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for target_day in pd.date_range(start, end, freq="D").date:
        prediction = predict_day(history, target_day, timezone)
        actual = history[history.day == target_day][["hour", "load_kwh"]]
        frame = prediction.merge(actual, on="hour", how="inner")
        frame["day"] = target_day
        frame["day_type"] = "weekday" if target_day.weekday() < 5 else "weekend"
        rows.append(frame)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def predict_day(history: pd.DataFrame, target_day: date, timezone: ZoneInfo) -> pd.DataFrame:
    target_start = pd.Timestamp(datetime.combine(target_day, datetime.min.time(), timezone)).tz_convert("UTC")
    window_start = target_start - pd.Timedelta(days=LOOKBACK_DAYS)
    training = history[(history.timestamp < target_start) & (history.timestamp >= window_start)].copy()
    target_type = "weekday" if target_day.weekday() < 5 else "weekend"
    hours = pd.DataFrame({"hour": range(24)})
    if training.empty:
        return hours.assign(prediction_kwh=np.nan, type_weight=0.0)
    age = (target_start - training.timestamp).dt.total_seconds() / 86_400
    training["weight"] = np.exp(-np.log(2) * age / HALF_LIFE_DAYS)
    all_days = training.groupby("hour").apply(
        lambda rows: np.average(rows.load_kwh, weights=rows.weight), include_groups=False,
    ).rename("all_days_kwh")
    typed = training[training.day_type == target_type].groupby("hour").apply(
        lambda rows: np.average(rows.load_kwh, weights=rows.weight), include_groups=False,
    ).rename("type_kwh")
    type_weight = training[training.day_type == target_type].groupby("hour").weight.sum().rename("type_weight")
    result = hours.merge(all_days, on="hour", how="left").merge(typed, on="hour", how="left").merge(type_weight, on="hour", how="left")
    result["type_weight"] = result.type_weight.fillna(0.0)
    # Four effective observations are treated as the strength of the broad
    # all-days prior.  Weekend evidence therefore grows smoothly rather than
    # switching abruptly once a small number of weekends is available.
    blend = result.type_weight / (result.type_weight + PRIOR_EFFECTIVE_OBSERVATIONS)
    result["prediction_kwh"] = result.type_kwh.fillna(result.all_days_kwh) * blend + result.all_days_kwh * (1 - blend)
    return result[["hour", "prediction_kwh", "type_weight"]]


if __name__ == "__main__":
    main()
