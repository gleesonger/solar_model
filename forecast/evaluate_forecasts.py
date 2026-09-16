"""Compare stored raw and adjusted solar forecasts with actual PV output.

The report evaluates every stored forecast snapshot that has a completed
actual target hour.  Results are also split by forecast lead time, avoiding a
misleading comparison between an intraday adjusted forecast and a day-ahead
raw forecast.

Examples:
    python -m forecast.evaluate_forecasts --database solar.db
    python -m forecast.evaluate_forecasts --database solar.db --date 2026-09-15
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


SCALE = 10_000
PANEL_IDS = (1, 2, 3, 4)
LEAD_BANDS = (
    ("0-1h", 0, 1),
    ("1-3h", 1, 3),
    ("3-6h", 3, 6),
    ("6-24h", 6, 24),
    ("24h+", 24, None),
)


def existing_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def forecast_performance(
    database_path: str | Path,
    *,
    timezone_name: str = "Europe/Dublin",
    target_date: str | None = None,
) -> pd.DataFrame:
    """Return one comparable forecast/actual row for each stored snapshot target."""
    connection = sqlite3.connect(database_path)
    try:
        forecast_columns = existing_columns(connection, "forecast_solar")
        raw_columns = [f"panel_{panel}_raw_watts" for panel in PANEL_IDS if f"panel_{panel}_raw_watts" in forecast_columns]
        adjusted_columns = [f"panel_{panel}_adj_watts" for panel in PANEL_IDS if f"panel_{panel}_adj_watts" in forecast_columns]
        if not raw_columns or not adjusted_columns:
            raise ValueError("forecast_solar must contain raw and adjusted watt columns; run the schema migration first")
        fields = ", ".join(("collected_at_utc", "forecast_time", *raw_columns, *adjusted_columns))
        forecasts = pd.read_sql_query(f"SELECT {fields} FROM forecast_solar", connection)
        actuals = pd.read_sql_query(
            "SELECT collected_at_utc, plant_pv_power_kw FROM sigenstor "
            "WHERE plant_pv_power_kw IS NOT NULL",
            connection,
        )
    finally:
        connection.close()

    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    forecasts["snapshot"] = pd.to_datetime(forecasts.pop("collected_at_utc"), unit="s", utc=True)
    forecasts["target"] = pd.to_datetime(forecasts.pop("forecast_time"), unit="s", utc=True).dt.floor("h")
    raw_values = forecasts[raw_columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    adjusted_values = forecasts[adjusted_columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    forecasts["raw_kw"] = raw_values.sum(axis=1) / SCALE / 1_000
    forecasts["adjusted_kw"] = adjusted_values.sum(axis=1) / SCALE / 1_000
    # A null adjusted value means that the backfill/live adjustment has not
    # completed, rather than a zero-production adjusted forecast.
    forecasts = forecasts[forecasts[adjusted_columns].notna().any(axis=1)].copy()

    actuals["target"] = pd.to_datetime(actuals.pop("collected_at_utc"), unit="s", utc=True).dt.floor("h")
    actual_hourly_kw = actuals.groupby("target", as_index=False)["plant_pv_power_kw"].mean()
    actual_hourly_kw["actual_kw"] = actual_hourly_kw.pop("plant_pv_power_kw") / SCALE
    result = forecasts.merge(actual_hourly_kw, on="target", how="inner")
    result = result[result["snapshot"] <= result["target"]].copy()
    result["lead_hours"] = (result["target"] - result["snapshot"]).dt.total_seconds() / 3_600
    if target_date is not None:
        try:
            requested_date = pd.Timestamp(target_date).date()
        except ValueError as error:
            raise ValueError("--date must use YYYY-MM-DD") from error
        result = result[result["target"].dt.tz_convert(ZoneInfo(timezone_name)).dt.date == requested_date].copy()
    return result


def metric_row(label: str, data: pd.DataFrame) -> dict[str, float | int | str]:
    actual = data["actual_kw"].to_numpy()
    raw_error = data["raw_kw"].to_numpy() - actual
    adjusted_error = data["adjusted_kw"].to_numpy() - actual
    raw_mae = float(np.mean(np.abs(raw_error)))
    adjusted_mae = float(np.mean(np.abs(adjusted_error)))
    return {
        "lead": label,
        "samples": len(data),
        "raw_mae_kw": raw_mae,
        "adjusted_mae_kw": adjusted_mae,
        "mae_improvement_percent": (raw_mae - adjusted_mae) / raw_mae * 100 if raw_mae else 0.0,
        "raw_rmse_kw": float(np.sqrt(np.mean(np.square(raw_error)))),
        "adjusted_rmse_kw": float(np.sqrt(np.mean(np.square(adjusted_error)))),
        "raw_bias_kw": float(np.mean(raw_error)),
        "adjusted_bias_kw": float(np.mean(adjusted_error)),
    }


def performance_report(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    rows = [metric_row("All", data)]
    for label, minimum, maximum in LEAD_BANDS:
        band = data[data["lead_hours"] >= minimum]
        if maximum is not None:
            band = band[band["lead_hours"] < maximum]
        if not band.empty:
            rows.append(metric_row(label, band))
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare raw and adjusted solar forecasts with actual PV power")
    parser.add_argument("--database", default="solar.db", help="SQLite database path")
    parser.add_argument("--date", help="Evaluate target hours on this local date (YYYY-MM-DD)")
    parser.add_argument("--timezone", default="Europe/Dublin", help="Timezone used by --date")
    parser.add_argument("--csv", help="Optional CSV path for the summary report")
    args = parser.parse_args()

    data = forecast_performance(args.database, timezone_name=args.timezone, target_date=args.date)
    report = performance_report(data)
    if report.empty:
        print("No completed adjusted forecast/actual pairs match the requested range.")
        return
    print(report.to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    if args.csv:
        report.to_csv(args.csv, index=False)


if __name__ == "__main__":
    main()
