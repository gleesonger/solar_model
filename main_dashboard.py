from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui
from nicegui.elements.column import Column
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from config import SolarArrayConfig, load_config
from database import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample


PvStringReader = Callable[[SigenStorModbusSample], tuple[float | None, float | None]]
PV_STRING_READERS: dict[str, PvStringReader] = {
    "pv1": lambda row: (row.inverter_pv1_voltage_volts, row.inverter_pv1_current_amps),
    "pv2": lambda row: (row.inverter_pv2_voltage_volts, row.inverter_pv2_current_amps),
    "pv3": lambda row: (row.inverter_pv3_voltage_volts, row.inverter_pv3_current_amps),
    "pv4": lambda row: (row.inverter_pv4_voltage_volts, row.inverter_pv4_current_amps),
}
ForecastWattHoursReader = Callable[[ForecastSolarSample], float | None]
FORECAST_WATT_HOURS_READERS: dict[str, ForecastWattHoursReader] = {
    "panel_1": lambda row: row.panel_1_watt_hours,
    "panel_2": lambda row: row.panel_2_watt_hours,
    "panel_3": lambda row: row.panel_3_watt_hours,
    "panel_4": lambda row: row.panel_4_watt_hours,
}
PANEL_COLORS = ("#5470c6", "#91cc75", "#fac858", "#ee6666")


def main() -> None:
    config = load_config()
    database_path = config.database.path
    timezone_name = config.timezone
    forecast_arrays = config.forecast.arrays
    actuals_to_forecast = config.dashboard.actuals_to_forecast

    @ui.page("/")
    def page() -> None:
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            render_dashboard(container, database_path, timezone_name, forecast_arrays, actuals_to_forecast)
        ui.timer(
            60,
            lambda: render_dashboard(
                container, database_path, timezone_name, forecast_arrays, actuals_to_forecast
            ),
        )

    ui.run(
        host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),
        port=int(os.getenv("DASHBOARD_PORT", "8080")),
        title="Solar dashboard",
        reload=False,
    )


def render_dashboard(
    container: Column,
    database_path: str,
    timezone_name: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> None:
    container.clear()
    with container:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Solar dashboard").classes("text-2xl font-bold")
            ui.button(
                "Refresh",
                on_click=lambda: render_dashboard(
                    container, database_path, timezone_name, forecast_arrays, actuals_to_forecast
                ),
                icon="refresh",
            )
        updated_at = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        ui.label(f"Last updated: {updated_at}").classes("text-sm text-gray-600")
        try:
            data = load_day(
                database_path, timezone_name, forecast_arrays, actuals_to_forecast
            ).copy()
            power_15m, battery_15m, latest = load_telemetry(database_path, timezone_name)
            device_information = load_device_information(database_path)
        except Exception as error:
            ui.label(f"Unable to load database: {error}").classes("text-red-600")
            return

        summary_columns = [
            {"name": "metric", "label": "", "field": "metric", "align": "left"},
            {"name": "latest", "label": "Latest (kW)", "field": "latest", "align": "right"},
            {"name": "today", "label": "Today (kWh)", "field": "today", "align": "right"},
            {"name": "forecast", "label": "Forecast (kWh)", "field": "forecast", "align": "right"},
        ]
        ui.table(
            columns=summary_columns,
            rows=summary_rows(data, latest),
            row_key="metric",
        ).props("dense flat bordered").classes("w-full max-w-3xl")

        ui.label("System information").classes("text-lg font-semibold mt-4")
        ui.table(
            columns=[
                {"name": "variable", "label": "Variable", "field": "variable", "align": "left"},
                {"name": "value", "label": "Value", "field": "value", "align": "right"},
                {"name": "unit", "label": "Unit", "field": "unit", "align": "left"},
            ],
            rows=device_information,
            row_key="variable",
        ).props("dense flat bordered").classes("w-full max-w-3xl")

        ui.label("Hourly PV energy (kWh)").classes("text-lg font-semibold")
        panel_names = {array.panel: array.name for array in forecast_arrays}
        forecast_panels = [array.panel for array in forecast_arrays]
        mapped_panels = set(actuals_to_forecast.values())
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {
                "data": [
                    "Solar (Actual)",
                    "Solar (Forecast)",
                    "Load",
                    "Grid Imported",
                    "Grid Exported",
                ]
            },
            "xAxis": {"type": "category", "data": dataframe_column(data, "hour").tolist()},
            "yAxis": {"type": "value", "name": "kWh"},
            "series": [
                {"name": "Solar (Actual)", "type": "bar", "data": chart_values(dataframe_column(data, "solar"))},
                {"name": "Solar (Forecast)", "type": "bar", "data": chart_values(dataframe_column(data, "forecast_total"))},
                {"name": "Load", "type": "bar", "data": chart_values(dataframe_column(data, "load"))},
                {"name": "Grid Imported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_import"))},
                {"name": "Grid Exported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_export"))},
            ],
        }).classes("w-full h-96")

        ui.label("15-minute average power (kW)").classes("text-lg font-semibold mt-4")
        power_series = [
            ("Solar", "solar"),
            ("Load", "load"),
            ("Battery", "battery"),
            ("Inverter", "inverter"),
            ("Grid Imported", "grid_import"),
            ("Grid Exported", "grid_export"),
        ]
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"data": [label for label, _ in power_series]},
            "xAxis": {"type": "category", "data": dataframe_column(power_15m, "time").tolist()},
            "yAxis": {"type": "value", "name": "kW"},
            "series": [
                {
                    "name": label,
                    "type": "line",
                    "showSymbol": False,
                    "connectNulls": False,
                    "data": chart_values(dataframe_column(power_15m, column)),
                }
                for label, column in power_series
            ],
        }).classes("w-full h-96")

        ui.label("Battery energy and state of charge").classes("text-lg font-semibold mt-4")
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"data": ["Available energy", "Rated capacity", "State of charge"]},
            "xAxis": {"type": "category", "data": dataframe_column(battery_15m, "time").tolist()},
            "yAxis": [
                {"type": "value", "name": "kWh"},
                {"type": "value", "name": "%", "min": 0, "max": 100},
            ],
            "series": [
                {
                    "name": "Available energy",
                    "type": "line",
                    "showSymbol": False,
                    "yAxisIndex": 0,
                    "data": chart_values(dataframe_column(battery_15m, "available_energy_kwh")),
                },
                {
                    "name": "Rated capacity",
                    "type": "line",
                    "showSymbol": False,
                    "yAxisIndex": 0,
                    "data": chart_values(dataframe_column(battery_15m, "rated_capacity_kwh")),
                },
                {
                    "name": "State of charge",
                    "type": "line",
                    "showSymbol": False,
                    "yAxisIndex": 1,
                    "data": chart_values(dataframe_column(battery_15m, "soc_percent")),
                },
            ],
        }).classes("w-full h-96")

        ui.label("Hourly solar energy by array (kWh)").classes("text-lg font-semibold mt-4")
        array_series: list[dict[str, Any]] = []
        for panel_index, panel_id in enumerate(forecast_panels):
            panel_name = panel_names[panel_id]
            panel_color = PANEL_COLORS[panel_index % len(PANEL_COLORS)]
            actual_values = (
                chart_values(dataframe_column(data, actual_column_name(panel_id)))
                if panel_id in mapped_panels
                else [None] * len(data)
            )
            array_series.extend([
                {
                    "name": f"{panel_name} (Actual)",
                    "type": "bar",
                    "data": actual_values,
                    "itemStyle": {"color": panel_color},
                },
                {
                    "name": f"{panel_name} (Forecast)",
                    "type": "bar",
                    "data": chart_values(dataframe_column(data, forecast_column_name(panel_id))),
                    "itemStyle": {
                        "color": panel_color,
                        "decal": {
                            "symbol": "rect",
                            "symbolSize": 1,
                            "color": "rgba(0, 0, 0, 0.35)",
                            "backgroundColor": "rgba(0, 0, 0, 0)",
                            "dashArrayX": [1, 0],
                            "dashArrayY": [3, 4],
                            "rotation": -0.7853981633974483,
                        },
                    },
                },
            ])
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"data": [series["name"] for series in array_series]},
            "xAxis": {"type": "category", "data": dataframe_column(data, "hour").tolist()},
            "yAxis": {"type": "value", "name": "kWh"},
            "series": array_series,
        }).classes("w-full h-96")

        table_columns = [
            "hour",
            "solar",
            "forecast_total",
            "load",
            "battery",
            "grid_import",
            "grid_export",
        ]
        table_labels = {
            "hour": "Hour",
            "solar": "Solar (Actual)",
            "forecast_total": "Solar (Forecast)",
            "load": "Load",
            "battery": "Battery (+ charge / - discharge)",
            "grid_import": "Grid import",
            "grid_export": "Grid export",
        }
        table_data = cast(pd.DataFrame, data[table_columns]).copy()
        table_rows = dataframe_rows(table_data)
        total: dict[str, Any] = {"hour": "Total"}
        total.update({
            column: round(float(dataframe_column(data, column).sum()), 3)
            for column in table_columns
            if column != "hour"
        })
        table_rows.append(total)
        ui.label("Hourly energy (kWh)").classes("text-lg font-semibold mt-4")
        ui.table(
            columns=[
                {"name": column, "label": table_labels[column], "field": column, "align": "right" if column != "hour" else "left"}
                for column in table_columns
            ],
            rows=table_rows,
            row_key="hour",
        ).classes("w-full")


def load_day(
    database_path: str,
    timezone_name: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> pd.DataFrame:
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            forecast_rows = load_forecast_for_day(session, day_start)
            actual = load_actual_hourly(session, day_start)
            actual_by_array = load_actual_arrays_hourly(session, day_start, actuals_to_forecast)
    finally:
        engine.dispose()

    forecast = aggregate_forecast(forecast_rows, day_start, forecast_arrays)
    hourly = actual.merge(actual_by_array, on="hour", how="left").merge(forecast, on="hour", how="left").fillna(0.0)
    return hourly


def load_telemetry(
    database_path: str,
    timezone_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float | None]]:
    timezone = ZoneInfo(timezone_name)
    day_start = datetime.now(timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    == day_start.strftime("%Y-%m-%d")
                )
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
    finally:
        engine.dispose()

    power_totals: dict[tuple[str, str], float] = {}
    power_counts: dict[tuple[str, str], int] = {}
    battery_totals: dict[tuple[str, str], float] = {}
    battery_counts: dict[tuple[str, str], int] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone)
        power_bucket = quarter_hour_label(timestamp)
        grid_power = sample.plant_grid_power_kw
        power_values = {
            "solar": sample.plant_pv_power_kw,
            "load": sample.plant_load_power_kw,
            "battery": sample.plant_battery_power_kw,
            "inverter": sample.inverter_power_kw,
            "grid_import": max(grid_power, 0.0) if grid_power is not None else None,
            "grid_export": max(-grid_power, 0.0) if grid_power is not None else None,
        }
        for name, value in power_values.items():
            if value is not None:
                key = (power_bucket, name)
                power_totals[key] = power_totals.get(key, 0.0) + value
                power_counts[key] = power_counts.get(key, 0) + 1

        battery_values = {
            "available_energy_kwh": sample.inverter_battery_available_discharge_kwh,
            "rated_capacity_kwh": sample.inverter_battery_rated_capacity_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for name, value in battery_values.items():
            if value is not None:
                key = (power_bucket, name)
                battery_totals[key] = battery_totals.get(key, 0.0) + value
                battery_counts[key] = battery_counts.get(key, 0) + 1

    bucket_labels = [quarter_hour_label(day_start + timedelta(minutes=15 * index)) for index in range(96)]
    power_rows: list[dict[str, Any]] = []
    battery_rows: list[dict[str, Any]] = []
    for bucket in bucket_labels:
        power_row: dict[str, Any] = {"time": bucket}
        for name in ("solar", "load", "battery", "inverter", "grid_import", "grid_export"):
            key = (bucket, name)
            count = power_counts.get(key, 0)
            power_row[name] = power_totals.get(key, 0.0) / count if count else None
        power_rows.append(power_row)
        battery_row: dict[str, Any] = {"time": bucket}
        for name in ("available_energy_kwh", "rated_capacity_kwh", "soc_percent"):
            key = (bucket, name)
            count = battery_counts.get(key, 0)
            battery_row[name] = battery_totals.get(key, 0.0) / count if count else None
        battery_rows.append(battery_row)

    latest_sample = samples[-1] if samples else None
    latest_grid = latest_sample.plant_grid_power_kw if latest_sample is not None else None
    latest = {
        "solar": latest_sample.plant_pv_power_kw if latest_sample is not None else None,
        "battery": latest_sample.plant_battery_power_kw if latest_sample is not None else None,
        "inverter": latest_sample.inverter_power_kw if latest_sample is not None else None,
        "inverter_today": latest_sample.inverter_pv_daily_kwh if latest_sample is not None else None,
        "load": latest_sample.plant_load_power_kw if latest_sample is not None else None,
        "grid_import": max(latest_grid, 0.0) if latest_grid is not None else None,
        "grid_export": max(-latest_grid, 0.0) if latest_grid is not None else None,
    }
    return pd.DataFrame(power_rows), pd.DataFrame(battery_rows), latest


def load_device_information(database_path: str) -> list[dict[str, str]]:
    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(SigenStorDevice).order_by(SigenStorDevice.id.desc())
            ).all()
    finally:
        engine.dispose()

    latest_by_variable: dict[str, SigenStorDevice] = {}
    for row in rows:
        latest_by_variable.setdefault(row.variable, row)
    return [
        {"variable": row.variable, "value": row.value, "unit": row.unit}
        for row in sorted(latest_by_variable.values(), key=lambda item: item.variable.casefold())
    ]


def load_forecast_for_day(session: Session, day_start: datetime) -> list[ForecastSolarSample]:
    first_forecast = session.scalar(
        select(ForecastSolarSample.collection_guid)
        .where(ForecastSolarSample.collection_guid.is_not(None))
        .where(func.substr(ForecastSolarSample.collected_at_local, 1, 10) == day_start.strftime("%Y-%m-%d"))
        .order_by(ForecastSolarSample.collected_at_utc.asc())
        .limit(1)
    )
    if not first_forecast:
        return []
    return list(session.scalars(
        select(ForecastSolarSample).where(ForecastSolarSample.collection_guid == first_forecast)
    ).all())


def load_actual_hourly(session: Session, day_start: datetime) -> pd.DataFrame:
    local_date = day_start.strftime("%Y-%m-%d")
    local_timestamp = func.substr(SigenStorModbusSample.collected_at_local, 1, 19)
    hour = func.strftime("%H", func.datetime(local_timestamp, "-1 second")).label("hour_key")
    query = (
        select(
            hour,
            func.sum(SigenStorModbusSample.plant_pv_total_kwh_period).label("solar"),
            func.sum(SigenStorModbusSample.plant_load_total_kwh_period).label("load"),
            func.sum(SigenStorModbusSample.plant_battery_charge_total_kwh_period).label("battery_charge"),
            func.sum(SigenStorModbusSample.plant_battery_discharge_total_kwh_period).label("battery_discharge"),
            func.sum(SigenStorModbusSample.plant_grid_import_total_kwh_period).label("grid_import"),
            func.sum(SigenStorModbusSample.plant_grid_export_total_kwh_period).label("grid_export"),
        )
        .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) == local_date)
        .where(SigenStorModbusSample.collected_at_local > day_start.isoformat())
        .group_by(hour)
    )
    grouped = pd.DataFrame(session.execute(query).mappings().all())
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    if grouped.empty:
        return hours.assign(solar=0.0, load=0.0, battery=0.0, grid_import=0.0, grid_export=0.0)

    grouped["hour"] = grouped["hour_key"].map(lambda value: f"{int(value):02d}:00")
    grouped["battery"] = grouped["battery_charge"].fillna(0) - grouped["battery_discharge"].fillna(0)
    grouped = grouped[["hour", "solar", "load", "battery", "grid_import", "grid_export"]]
    return hours.merge(grouped, on="hour", how="left").fillna(0.0)


def load_actual_arrays_hourly(
    session: Session,
    day_start: datetime,
    actuals_to_forecast: dict[str, str],
) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = {actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()}
    if not actuals_to_forecast:
        return hours

    samples = list(session.scalars(
        select(SigenStorModbusSample)
        .where(
            func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
            == day_start.strftime("%Y-%m-%d")
        )
        .order_by(SigenStorModbusSample.collected_at_utc)
    ).all())
    totals: dict[tuple[str, str], float] = {}
    previous_timestamp: datetime | None = None
    previous_power: dict[str, float | None] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone)
        current_power: dict[str, float | None] = {}
        for pv_string in actuals_to_forecast:
            voltage, current = PV_STRING_READERS[pv_string](sample)
            current_power[pv_string] = (
                max(voltage * current, 0.0) / 1000
                if voltage is not None and current is not None
                else None
            )

        if previous_timestamp is not None:
            duration_hours = (timestamp - previous_timestamp).total_seconds() / 3600
            hour = format_hour(timestamp - timedelta(microseconds=1), day_start)
            if hour is not None and duration_hours > 0:
                for pv_string, panel_id in actuals_to_forecast.items():
                    before = previous_power.get(pv_string)
                    after = current_power[pv_string]
                    if before is not None and after is not None:
                        column = actual_column_name(panel_id)
                        totals[(hour, column)] = totals.get((hour, column), 0.0) + (
                            (before + after) / 2 * duration_hours
                        )

        previous_timestamp = timestamp
        previous_power = current_power

    for column in actual_columns:
        hours[column] = dataframe_column(hours, "hour").map(
            lambda hour, column=column: totals.get((hour, column), 0.0)
        )
    return hours


def aggregate_forecast(
    rows: list[ForecastSolarSample],
    day_start: datetime,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    points: list[dict[str, object]] = []
    for row in rows:
        try:
            timestamp = parse_time(row.forecast_time, timezone)
        except (TypeError, ValueError):
            continue
        for array in forecast_arrays:
            watt_hours = FORECAST_WATT_HOURS_READERS[array.panel](row)
            if watt_hours is not None:
                points.append({"timestamp": timestamp, "panel_id": array.panel, "watt_hours": watt_hours})

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    forecast_columns = [forecast_column_name(array.panel) for array in forecast_arrays]
    if not points:
        return hours.assign(**{column: 0.0 for column in [*forecast_columns, "forecast_total"]})

    frame = pd.DataFrame(points).sort_values(["panel_id", "timestamp"])
    frame["forecast_date"] = frame["timestamp"].dt.date
    frame["energy_kwh"] = (
        frame.groupby(["panel_id", "forecast_date"])["watt_hours"]
        .diff()
        .fillna(frame["watt_hours"])
        .clip(lower=0)
        .div(1000)
    )
    frame["hour"] = frame["timestamp"].map(
        lambda value: format_hour(value - timedelta(microseconds=1), day_start)
    )
    frame = frame.dropna(subset=["hour"])
    grouped = frame.pivot_table(index="hour", columns="panel_id", values="energy_kwh", aggfunc="sum", fill_value=0).reset_index()
    grouped = grouped.rename(columns={
        array.panel: forecast_column_name(array.panel) for array in forecast_arrays
    })
    for column in forecast_columns:
        if column not in grouped:
            grouped[column] = 0.0
    grouped["forecast_total"] = grouped[forecast_columns].sum(axis=1)
    return hours.merge(grouped[["hour", *forecast_columns, "forecast_total"]], on="hour", how="left").fillna(0.0)


def parse_time(value: str, timezone: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def hour_index(value: datetime, day_start: datetime) -> int | None:
    index = int((value - day_start).total_seconds() // 3600)
    return index if 0 <= index < 24 else None


def format_hour(value: datetime, day_start: datetime) -> str | None:
    index = hour_index(value, day_start)
    return f"{index:02d}:00" if index is not None else None


def actual_column_name(panel_id: str) -> str:
    return f"actual_{panel_id}"


def forecast_column_name(panel_id: str) -> str:
    return f"forecast_{panel_id}"


def quarter_hour_label(value: datetime) -> str:
    minute = value.minute // 15 * 15
    return f"{value.hour:02d}:{minute:02d}"


def summary_rows(
    data: pd.DataFrame,
    latest: dict[str, float | None],
) -> list[dict[str, str]]:
    today = {
        "solar": float(dataframe_column(data, "solar").sum()),
        "battery": float(dataframe_column(data, "battery").sum()),
        "inverter": latest["inverter_today"],
        "load": float(dataframe_column(data, "load").sum()),
        "grid_import": float(dataframe_column(data, "grid_import").sum()),
        "grid_export": float(dataframe_column(data, "grid_export").sum()),
    }
    forecast = {
        "solar": float(dataframe_column(data, "forecast_total").sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    labels = [
        ("Solar", "solar"),
        ("Battery", "battery"),
        ("Inverter", "inverter"),
        ("Load", "load"),
        ("Grid-Imported", "grid_import"),
        ("Grid-Exported", "grid_export"),
    ]
    return [
        {
            "metric": label,
            "latest": format_dashboard_number(latest[key]),
            "today": format_dashboard_number(today[key]),
            "forecast": format_dashboard_number(forecast[key]),
        }
        for label, key in labels
    ]


def format_dashboard_number(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def chart_values(values: pd.Series) -> list[float | None]:
    return [None if pd.isna(value) else round(float(value), 3) for value in values]


def dataframe_column(frame: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, frame[name])


def dataframe_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = frame.round(3).to_dict(orient="records")
    return [{key: (None if pd.isna(value) else value) for key, value in row.items()} for row in rows]


if __name__ == "__main__":
    main()
