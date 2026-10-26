"""Backtest a recency-weighted hourly household-load profile.

This report evaluates the production daily load model without writing forecasts. Its YAML-configured defaults use a six-week lookback
and a two-week half-life.  The local weekday/weekend hourly estimate is
shrunk towards the all-days hourly estimate, so scarce weekend observations
benefit from weekday history without being treated as identical to weekdays.
"""
from __future__ import annotations

import argparse
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd


from solar_model.config import ForecastModelConfig, load_config
from solar_model.storage.queries.forecasting import load_hourly_house_load
from solar_model.planning.forecasts.house_load import predict_day


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the hierarchical hourly load profile")
    parser.add_argument("--config", default="config.yaml", help="YAML configuration file")
    parser.add_argument("--database", help="Override the configured database")
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--timezone", help="Override the configured timezone")
    args = parser.parse_args()
    config = load_config(args.config)
    timezone = ZoneInfo(args.timezone or config.timezone)
    result = evaluate(load_hourly_house_load(args.database or config.database.path, timezone), args.start, args.end, timezone, config.forecast.model_calibration_parameters.load)
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


def evaluate(history: pd.DataFrame, start: date, end: date, timezone: ZoneInfo, settings: ForecastModelConfig | None = None) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for target_day in pd.date_range(start, end, freq="D").date:
        prediction = predict_day(history, target_day, timezone, settings)
        actual = history[history.day == target_day][["hour", "load_kwh"]]
        frame = prediction.merge(actual, on="hour", how="inner")
        frame["day"] = target_day
        frame["day_type"] = "weekday" if target_day.weekday() < 5 else "weekend"
        rows.append(frame)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


if __name__ == "__main__":
    main()
