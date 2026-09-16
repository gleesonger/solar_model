"""Train and evaluate load and solar-correction models from solar.db.

The script deliberately uses chronological, whole-day train/validation/test
splits.  A solar feature row uses the most recent forecast snapshot collected
before 00:00 UTC on its target day, which prevents later forecast refreshes
from leaking into historical training examples.

Usage:
    python forecast/train_models.py
    python forecast/train_models.py --database forecast/solar.db --save-dir forecast/models
"""

from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error


SCALE = 10_000  # database.py's ScaledInteger multiplier
RANDOM_STATE = 42


@dataclass
class Scores:
    rows: int
    mae_kw: float
    rmse_kw: float
    energy_bias_kwh: float


def read_actuals(connection: sqlite3.Connection, timezone_name: str = "Europe/Dublin") -> pd.DataFrame:
    """Return hourly mean actual load and PV in the site's local timezone."""
    actuals = pd.read_sql_query(
        """SELECT collected_at_utc, plant_load_power_kw, plant_pv_power_kw
           FROM sigenstor
           WHERE plant_load_power_kw IS NOT NULL AND plant_pv_power_kw IS NOT NULL
           ORDER BY collected_at_utc""",
        connection,
    )
    actuals["timestamp"] = pd.to_datetime(actuals.pop("collected_at_utc"), unit="s", utc=True).dt.tz_convert(timezone_name)
    actuals[["plant_load_power_kw", "plant_pv_power_kw"]] /= SCALE
    return (actuals.set_index("timestamp")[["plant_load_power_kw", "plant_pv_power_kw"]]
            .resample("1h").mean().rename(columns={
                "plant_load_power_kw": "load_kw", "plant_pv_power_kw": "pv_kw"}))


def calendar_features(index: pd.DatetimeIndex, include_weekday: bool = True) -> pd.DataFrame:
    """Calendar and solar-position proxy features available without APIs."""
    hour = index.hour + index.minute / 60
    day = index.dayofyear
    result = pd.DataFrame({
        # Retain a discrete local hour as well as cyclic values, allowing tree
        # splits to learn a distinct 07:00 dawn calibration.
        "hour_local": hour,
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
        "day_sin": np.sin(2 * np.pi * day / 365.25),
        "day_cos": np.cos(2 * np.pi * day / 365.25),
    }, index=index)
    if include_weekday:
        weekday = index.dayofweek
        result["weekday_sin"] = np.sin(2 * np.pi * weekday / 7)
        result["weekday_cos"] = np.cos(2 * np.pi * weekday / 7)
        result["weekend"] = (weekday >= 5).astype(int)
    return result


def load_features(actuals: pd.DataFrame) -> pd.DataFrame:
    result = calendar_features(actuals.index)
    # A fortnightly lag is valuable once enough history exists, but including it
    # from day one would discard two thirds of this currently short history.
    lags = [1, 2, 24, 48, 168]
    if len(actuals) >= 24 * 42:
        lags.append(336)
    for hours in lags:
        result[f"load_lag_{hours}h"] = actuals["load_kw"].shift(hours)
    for hours in (1, 3, 6, 24):
        # shift ensures the current/future load is never used as a feature.
        result[f"load_mean_{hours}h"] = actuals["load_kw"].shift(1).rolling(hours).mean()
        result[f"load_std_{hours}h"] = actuals["load_kw"].shift(1).rolling(hours).std(ddof=0)
    result["target_load_kw"] = actuals["load_kw"]
    # Baseline: mean load at this *local clock hour* across the preceding 14
    # days. DateOffset preserves the local hour over DST transitions.
    prior_days = pd.concat(
        [actuals["load_kw"].shift(freq=pd.DateOffset(days=days)) for days in range(1, 15)],
        axis=1,
    )
    result["baseline_load_kw"] = prior_days.mean(axis=1).where(prior_days.count(axis=1) == 14)
    # The baseline is an evaluation/fallback forecast, not a training input;
    # do not discard otherwise valid early model rows because it is unavailable.
    return result.dropna(subset=[column for column in result if column != "baseline_load_kw"])


def solar_features(connection: sqlite3.Connection, actuals: pd.DataFrame, timezone_name: str = "Europe/Dublin") -> pd.DataFrame:
    """Make one solar row per target hour using only a pre-midnight snapshot."""
    forecasts = pd.read_sql_query(
        """SELECT collected_at_utc, forecast_time, panel_1_raw_watts, panel_2_raw_watts,
                  panel_1_raw_watt_hours_day, panel_2_raw_watt_hours_day
           FROM forecast_solar
           WHERE panel_1_raw_watts IS NOT NULL OR panel_2_raw_watts IS NOT NULL""",
        connection,
    )
    forecasts["snapshot"] = pd.to_datetime(forecasts.pop("collected_at_utc"), unit="s", utc=True).dt.tz_convert(timezone_name)
    forecasts["target"] = pd.to_datetime(forecasts.pop("forecast_time"), unit="s", utc=True).dt.tz_convert(timezone_name).dt.floor("h")
    numeric = ["panel_1_raw_watts", "panel_2_raw_watts", "panel_1_raw_watt_hours_day", "panel_2_raw_watt_hours_day"]
    forecasts[numeric] = forecasts[numeric].fillna(0) / SCALE
    forecasts["target_day"] = forecasts["target"].dt.normalize()
    # Snapshots must be available before the target day begins; choose latest.
    eligible = forecasts[forecasts["snapshot"] < forecasts["target_day"]].copy()
    eligible.sort_values(["target", "snapshot"], inplace=True)
    latest = eligible.groupby("target", as_index=False).tail(1).set_index("target")
    # forecast.solar reports these fields in W and Wh; actual inverter power
    # is in kW, so convert before constructing a common training target.
    latest["raw_solar_kw"] = (latest.panel_1_raw_watts + latest.panel_2_raw_watts) / 1_000
    latest["raw_solar_day_kwh"] = (latest.panel_1_raw_watt_hours_day + latest.panel_2_raw_watt_hours_day) / 1_000
    latest["forecast_lead_hours"] = (latest.index.to_series() - latest.snapshot).dt.total_seconds() / 3600
    result = latest.join(actuals[["pv_kw"]], how="inner")
    result = result.join(calendar_features(result.index, include_weekday=False))
    result["pv_lag_1h"] = actuals["pv_kw"].shift(1)
    result["pv_lag_24h"] = actuals["pv_kw"].shift(24)
    # Error history is similarly lagged, so it was observable at prediction time.
    result["residual_lag_1h"] = (actuals["pv_kw"] - result["raw_solar_kw"]).shift(1)
    result["residual_mean_3h"] = (actuals["pv_kw"] - result["raw_solar_kw"]).shift(1).rolling(3).mean()
    result["target_error_kw"] = result["pv_kw"] - result["raw_solar_kw"]
    # Multiplicative correction means zero raw generation naturally remains
    # zero. Bound rare low-light ratios for this small training set.
    result["target_solar_factor"] = np.where(
        result["raw_solar_kw"] > 0,
        (result["pv_kw"] / result["raw_solar_kw"]).clip(0, 3),
        0.0,
    )
    # HistGradientBoostingRegressor supports missing lag features. Keeping
    # them retains the essential 06:00–08:00 dawn examples.
    return result.dropna(subset=["target_solar_factor"])


def split_days(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """70/15/15 chronological split, maintaining whole-day boundaries."""
    days = pd.Index(frame.index.normalize().unique()).sort_values()
    if len(days) < 12:
        raise ValueError(f"Need at least 12 complete feature days; found {len(days)}")
    train_end = max(1, int(len(days) * .70))
    validation_end = max(train_end + 1, int(len(days) * .85))
    labels = frame.index.normalize()
    return (frame[labels.isin(days[:train_end])],
            frame[labels.isin(days[train_end:validation_end])],
            frame[labels.isin(days[validation_end:])])


def score(actual: pd.Series, predicted: np.ndarray) -> Scores:
    errors = actual.to_numpy() - predicted
    return Scores(len(actual), float(mean_absolute_error(actual, predicted)),
                  float(mean_squared_error(actual, predicted) ** .5), float(errors.sum()))


def evaluate(name: str, frame: pd.DataFrame, target: str, baseline: str, save_dir: Path) -> dict:
    train, validation, test = split_days(frame)
    exclude = {target, "pv_kw", "target_error_kw", "target_solar_factor", "snapshot", "target_day",
               "panel_1_raw_watts", "panel_2_raw_watts", "panel_1_raw_watt_hours_day", "panel_2_raw_watt_hours_day"}
    if name == "load":
        exclude.add(baseline)
    features = [column for column in frame.select_dtypes(include="number").columns if column not in exclude]
    model = HistGradientBoostingRegressor(max_iter=200, max_leaf_nodes=12, l2_regularization=1.0,
                                          learning_rate=.06, random_state=RANDOM_STATE)
    model.fit(train[features], train[target])

    results: dict[str, dict[str, dict]] = {"features": {"names": features}}
    for split_name, split in (("validation", validation), ("test", test)):
        prediction = model.predict(split[features])
        if name == "solar":
            raw = split["raw_solar_kw"].to_numpy()
            prediction = raw * np.maximum(0, prediction)
            actual = split["pv_kw"]
            baseline_prediction = split["raw_solar_kw"].to_numpy()
        else:
            actual = split[target]
            baseline_prediction = split[baseline].to_numpy()
        results[split_name] = {
            "model": asdict(score(actual, prediction)),
            "baseline": asdict(score(actual, baseline_prediction)),
        }
    save_dir.mkdir(parents=True, exist_ok=True)
    with (save_dir / f"{name}_model.pkl").open("wb") as handle:
        pickle.dump({"model": model, "features": features}, handle)
    return results


def print_results(name: str, result: dict) -> None:
    print(f"\n{name.upper()} ({len(result['features']['names'])} input features)")
    for split in ("validation", "test"):
        model, baseline = result[split]["model"], result[split]["baseline"]
        improvement = 100 * (1 - model["mae_kw"] / baseline["mae_kw"])
        print(f"  {split:10} model MAE {model['mae_kw']:.3f} kW, RMSE {model['rmse_kw']:.3f} kW, "
              f"energy bias {model['energy_bias_kwh']:.2f} kWh ({improvement:+.1f}% MAE vs baseline)")
        print(f"  {'':10} baseline MAE {baseline['mae_kw']:.3f} kW, RMSE {baseline['rmse_kw']:.3f} kW, "
              f"energy bias {baseline['energy_bias_kwh']:.2f} kWh; n={model['rows']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path(__file__).with_name("solar.db"))
    parser.add_argument("--save-dir", type=Path, default=Path(__file__).with_name("models"))
    parser.add_argument("--timezone", default="Europe/Dublin")
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        actuals = read_actuals(connection, args.timezone)
        load = load_features(actuals)
        solar = solar_features(connection, actuals, args.timezone)
    results = {
        "load": evaluate("load", load, "target_load_kw", "baseline_load_kw", args.save_dir),
        "solar": evaluate("solar", solar, "target_solar_factor", "raw_solar_kw", args.save_dir),
    }
    for name, result in results.items():
        print_results(name, result)
    (args.save_dir / "metrics.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nSaved model artifacts and metrics to {args.save_dir}")


if __name__ == "__main__":
    main()
