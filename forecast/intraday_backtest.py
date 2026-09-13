"""Simulate daily retraining and intraday load/PV forecasts on the test period.

At every test-day midnight the script fits fresh models using only earlier
feature rows.  It then forecasts the rest of that day at configurable cut-offs
(03:00 by default), exposing actual readings only from before each cut-off.
The workbook has one blank row between each day's forecast block.

Usage:
    python forecast/intraday_backtest.py
    python forecast/intraday_backtest.py --run-every-hours 1
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from train_models import calendar_features, load_features, read_actuals, solar_features, split_days


RANDOM_STATE = 42


def fit(frame: pd.DataFrame, target: str, excluded: set[str]) -> tuple[HistGradientBoostingRegressor, list[str]]:
    features = [name for name in frame.select_dtypes(include="number") if name not in excluded]
    model = HistGradientBoostingRegressor(max_iter=200, max_leaf_nodes=12, l2_regularization=1.0,
                                          learning_rate=.06, random_state=RANDOM_STATE)
    model.fit(frame[features], frame[target])
    return model, features


def value_at(series: pd.Series, timestamp: pd.Timestamp) -> float:
    return float(series.get(timestamp, np.nan))


def hour_delta(value: int) -> pd.Timedelta:
    """Avoid NumPy's deprecated unit-less timedelta conversion."""
    return pd.Timedelta(int(value), unit="h")


def load_row(history: pd.Series, timestamp: pd.Timestamp) -> pd.DataFrame:
    """Features for one future hour; history contains actuals and prior predictions."""
    result = calendar_features(pd.DatetimeIndex([timestamp]))
    for hours in (1, 2, 24, 48, 168):
        result[f"load_lag_{hours}h"] = value_at(history, timestamp - hour_delta(hours))
    past = history[history.index < timestamp]
    for hours in (1, 3, 6, 24):
        window = past.tail(hours)
        result[f"load_mean_{hours}h"] = window.mean() if len(window) == hours else np.nan
        result[f"load_std_{hours}h"] = window.std(ddof=0) if len(window) == hours else np.nan
    return result


def predict_load_day(model: HistGradientBoostingRegressor, features: list[str], actuals: pd.DataFrame,
                     day: pd.Timestamp, as_of_hour: int) -> list[float | None]:
    as_of = day + hour_delta(as_of_hour)
    # The value for the as-of hour is forecast; only earlier completed hours are known.
    history = actuals.loc[actuals.index < as_of, "load_kw"].dropna().copy()
    forecast: list[float | None] = [None] * 24
    for hour in range(as_of_hour, 24):
        timestamp = day + hour_delta(hour)
        row = load_row(history, timestamp).reindex(columns=features)
        prediction = max(0.0, float(model.predict(row)[0]))
        forecast[hour] = prediction
        history.loc[timestamp] = prediction
    return forecast


def forecast_source(connection: sqlite3.Connection, timezone_name: str) -> pd.DataFrame:
    """Return raw solar forecasts in kW/kWh, retaining each snapshot time."""
    source = pd.read_sql_query(
        """SELECT collected_at_utc, forecast_time, panel_1_watts, panel_2_watts,
                  panel_1_watt_hours_day, panel_2_watt_hours_day
           FROM forecast_solar""", connection)
    source["snapshot"] = pd.to_datetime(source.pop("collected_at_utc"), unit="s", utc=True).dt.tz_convert(timezone_name)
    source["target"] = pd.to_datetime(source.pop("forecast_time"), unit="s", utc=True).dt.tz_convert(timezone_name).dt.floor("h")
    fields = ["panel_1_watts", "panel_2_watts", "panel_1_watt_hours_day", "panel_2_watt_hours_day"]
    source[fields] = source[fields].fillna(0) / 10_000
    source["raw_solar_kw"] = (source.panel_1_watts + source.panel_2_watts) / 1_000
    source["raw_solar_day_kwh"] = (source.panel_1_watt_hours_day + source.panel_2_watt_hours_day) / 1_000
    return source.sort_values(["target", "snapshot"])


def raw_at(source: pd.DataFrame, as_of: pd.Timestamp, target: pd.Timestamp) -> tuple[float, float, float]:
    rows = source[(source.target == target) & (source.snapshot <= as_of)]
    if rows.empty:
        return np.nan, np.nan, np.nan
    row = rows.iloc[-1]
    return float(row.raw_solar_kw), float(row.raw_solar_day_kwh), (target - row.snapshot).total_seconds() / 3600


def solar_row(timestamp: pd.Timestamp, raw_kw: float, raw_day_kwh: float, lead_hours: float,
              pv_history: pd.Series, residual_history: pd.Series) -> pd.DataFrame:
    result = calendar_features(pd.DatetimeIndex([timestamp]), include_weekday=False)
    result["raw_solar_kw"] = raw_kw
    result["raw_solar_day_kwh"] = raw_day_kwh
    result["forecast_lead_hours"] = lead_hours
    result["pv_lag_1h"] = value_at(pv_history, timestamp - hour_delta(1))
    result["pv_lag_24h"] = value_at(pv_history, timestamp - hour_delta(24))
    result["residual_lag_1h"] = value_at(residual_history, timestamp - hour_delta(1))
    residuals = residual_history[residual_history.index < timestamp].tail(3)
    result["residual_mean_3h"] = residuals.mean() if len(residuals) == 3 else np.nan
    return result


def predict_solar_day(model: HistGradientBoostingRegressor, features: list[str], actuals: pd.DataFrame,
                      source: pd.DataFrame, day: pd.Timestamp, as_of_hour: int) -> list[float | None]:
    as_of = day + hour_delta(as_of_hour)
    pv_history = actuals.loc[actuals.index < as_of, "pv_kw"].dropna().copy()
    # Build residual history from forecasts that were genuinely available by this cut-off.
    residual_history = pd.Series(dtype=float)
    for timestamp, pv in pv_history.items():
        raw_kw, _, _ = raw_at(source, as_of, timestamp)
        if pd.notna(raw_kw):
            residual_history.loc[timestamp] = pv - raw_kw
    forecast: list[float | None] = [None] * 24
    for hour in range(as_of_hour, 24):
        timestamp = day + hour_delta(hour)
        raw_kw, raw_day_kwh, lead_hours = raw_at(source, as_of, timestamp)
        if pd.isna(raw_kw):
            # forecast.solar omits overnight target timestamps.  They are a
            # zero-generation forecast rather than an unknown model result.
            forecast[hour] = 0.0
            pv_history.loc[timestamp] = 0.0
            residual_history.loc[timestamp] = 0.0
            continue
        row = solar_row(timestamp, raw_kw, raw_day_kwh, lead_hours, pv_history, residual_history)
        factor = float(model.predict(row.reindex(columns=features))[0])
        # The multiplicative formulation enforces the physical relationship:
        # an explicit zero raw forecast gives zero corrected generation.
        prediction = raw_kw * max(0.0, factor)
        forecast[hour] = prediction
        # Recursive values allow later intraday feature rows to retain their normal shape.
        pv_history.loc[timestamp] = prediction
        residual_history.loc[timestamp] = prediction - raw_kw
    return forecast


def write_block(sheet, row: int, day: pd.Timestamp, actual: list[float], forecasts: list[tuple[int, list[float | None]]], fmt,
                raw_t0: list[float | None] | None = None) -> int:
    def spreadsheet_values(values):
        return [None if value is None or pd.isna(value) else value for value in values]

    sheet.write(row, 0, day.strftime("%Y-%m-%d"), fmt["date"])
    sheet.write(row, 1, "Actual", fmt["label"])
    sheet.write_row(row, 2, spreadsheet_values(actual), fmt["number"])
    row += 1
    if raw_t0 is not None:
        sheet.write(row, 1, "Raw forecast at 00:00 (T0)", fmt["label"])
        sheet.write_row(row, 2, spreadsheet_values(raw_t0), fmt["number"])
        row += 1
    for hour, values in forecasts:
        sheet.write(row, 1, f"Forecast as at {hour:02d}:00", fmt["label"])
        sheet.write_row(row, 2, spreadsheet_values(values), fmt["number"])
        row += 1
    return row + 1  # blank separator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path(__file__).with_name("solar.db"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("intraday_backtest.xlsx"))
    parser.add_argument("--run-every-hours", type=int, default=3, choices=range(1, 25))
    parser.add_argument("--timezone", default="Europe/Dublin")
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        actuals = read_actuals(connection, args.timezone)
        load = load_features(actuals)
        solar = solar_features(connection, actuals, args.timezone)
        source = forecast_source(connection, args.timezone)

    _, _, load_test = split_days(load)
    _, _, solar_test = split_days(solar)
    load_days = pd.Index(load_test.index.normalize().unique()).sort_values()
    solar_days = pd.Index(solar_test.index.normalize().unique()).sort_values()

    with pd.ExcelWriter(args.output, engine="xlsxwriter") as writer:
        workbook = writer.book
        fmt = {
            "header": workbook.add_format({"bold": True, "bg_color": "#D9EAD3", "align": "center"}),
            "date": workbook.add_format({"bold": True}),
            "label": workbook.add_format({"italic": True}),
            "number": workbook.add_format({"num_format": "0.000"}),
        }
        for name, days, frame, target, excluded, predictor, actual_column in (
            ("Load", load_days, load, "target_load_kw", {"target_load_kw", "baseline_load_kw"}, predict_load_day, "load_kw"),
            ("Solar", solar_days, solar, "target_solar_factor", {"target_error_kw", "target_solar_factor", "pv_kw", "snapshot", "target_day", "panel_1_watts", "panel_2_watts", "panel_1_watt_hours_day", "panel_2_watt_hours_day"}, predict_solar_day, "pv_kw"),
        ):
            sheet = workbook.add_worksheet(name)
            writer.sheets[name] = sheet
            sheet.write(0, 0, "Date", fmt["header"])
            sheet.write(0, 1, "Run", fmt["header"])
            for hour in range(24):
                sheet.write(0, hour + 2, f"{hour:02d}:00", fmt["header"])
            sheet.freeze_panes(1, 2)
            sheet.set_column(0, 1, 20)
            sheet.set_column(2, 25, 11)
            row = 1
            for day in days:
                training = frame[frame.index.normalize() < day]
                model, features = fit(frame=training, target=target, excluded=excluded)
                actual = [value_at(actuals[actual_column], day + hour_delta(hour)) for hour in range(24)]
                raw_t0 = None
                if name == "Solar":
                    # T0 is the uncorrected forecast available at local midnight.
                    raw_t0 = []
                    for hour in range(24):
                        raw_kw, _, _ = raw_at(source, day, day + hour_delta(hour))
                        raw_t0.append(0.0 if pd.isna(raw_kw) else raw_kw)
                forecasts = []
                for as_of_hour in range(0, 24, args.run_every_hours):
                    if name == "Load":
                        values = predictor(model, features, actuals, day, as_of_hour)
                    else:
                        values = predictor(model, features, actuals, source, day, as_of_hour)
                    forecasts.append((as_of_hour, values))
                row = write_block(sheet, row, day, actual, forecasts, fmt, raw_t0)
    print(f"Wrote {args.output} ({len(load_days)} load days, {len(solar_days)} solar days; "
          f"forecast runs every {args.run_every_hours} hour(s)).")


if __name__ == "__main__":
    main()
