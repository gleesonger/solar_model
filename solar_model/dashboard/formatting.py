"""Table labels, numeric rendering and chart-value presentation."""
import pandas as pd

from solar_model.storage.queries.dashboard import ENERGY_SUMMARY_COLUMNS, dataframe_column
from solar_model.storage.queries.types import SUMMARY_LATEST_KEYS


CURRENCY_SUMMARY_COLUMNS = frozenset({
    "grid_import_cost",
    "grid_export_revenue",
    "net_cost",
    "no_solar_battery_import_cost",
})

def battery_status_text(latest: dict[str, float | None]) -> str:
    energy = format_dashboard_number(latest["battery_available_energy_kwh"])
    soc = format_dashboard_number(latest["battery_soc_percent"])
    return f"Battery: {energy} kWh available, {soc}% SoC"

def energy_summary_rows(data: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {
            "period": str(row["period"]),
            **{
                column: (
                    format_currency(row[column])
                    if column in CURRENCY_SUMMARY_COLUMNS
                    else format_dashboard_number(row[column])
                )
                for column in ENERGY_SUMMARY_COLUMNS
            },
        }
        for row in data.to_dict(orient="records")
    ]

def summary_rows(
    data: pd.DataFrame,
    latest: dict[str, float | None],
    latest_keys: dict[str, str] = SUMMARY_LATEST_KEYS,
) -> list[dict[str, str]]:
    today = {
        "solar": float(dataframe_column(data, "solar").sum()),
        "battery": float(dataframe_column(data, "battery").sum()),
        "load": float(dataframe_column(data, "load").sum()),
        "grid_import": float(dataframe_column(data, "grid_import").sum()),
        "grid_export": float(dataframe_column(data, "grid_export").sum()),
    }
    latest_forecast = {
        "solar": float(dataframe_column(
            data, "latest_forecast_total" if "latest_forecast_total" in data else "forecast_total",
        ).sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    # Retain the existing `forecast` field name for the table/API, but its
    # definition is explicitly the beginning-of-day adjusted forecast.
    forecast = {
        "solar": float(dataframe_column(
            data, "forecast_bod_total" if "forecast_bod_total" in data else "forecast_total",
        ).sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    forecast_raw = {
        "solar": float(dataframe_column(data, "forecast_raw_total").sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    rows = [
        {
            "metric": label,
            "latest_key": key,
            "is_solar_orientation": key.startswith("pv"),
            "latest": format_dashboard_number(latest.get(key)),
            "today": format_dashboard_number(today.get(key)),
            "latest_forecast": format_dashboard_number(latest_forecast.get(key)),
            "forecast": format_dashboard_number(forecast.get(key)),
            "forecast_raw": format_dashboard_number(forecast_raw.get(key)),
        }
        for label, key in latest_keys.items()
    ]
    return rows

def format_dashboard_number(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    rounded = round(float(value), 1)
    return f"{0.0 if rounded == 0 else rounded:.1f}"

def format_currency(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.2f}"

def chart_values(values: pd.Series) -> list[float | None]:
    rounded_values: list[float | None] = []
    for value in values:
        if pd.isna(value):
            rounded_values.append(None)
            continue
        rounded = round(float(value), 1)
        rounded_values.append(0.0 if rounded == 0 else rounded)
    return rounded_values
