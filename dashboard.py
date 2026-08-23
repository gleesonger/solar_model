from __future__ import annotations

import os
import re
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

from config import load_config
from database import ForecastSolarSample, SigenStorModbusSample


PvStringReader = Callable[[SigenStorModbusSample], tuple[float | None, float | None]]
PV_STRING_READERS: dict[str, PvStringReader] = {
    "pv1": lambda row: (row.inverter_pv1_voltage_volts, row.inverter_pv1_current_amps),
    "pv2": lambda row: (row.inverter_pv2_voltage_volts, row.inverter_pv2_current_amps),
    "pv3": lambda row: (row.inverter_pv3_voltage_volts, row.inverter_pv3_current_amps),
    "pv4": lambda row: (row.inverter_pv4_voltage_volts, row.inverter_pv4_current_amps),
}


def main() -> None:
    config = load_config()
    database_path = config.database.path
    timezone_name = config.timezone
    pv_string_map = config.dashboard.pv_string_map

    @ui.page("/")
    def page() -> None:
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            render_dashboard(container, database_path, timezone_name, pv_string_map)
        ui.timer(60, lambda: render_dashboard(container, database_path, timezone_name, pv_string_map))

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
    pv_string_map: dict[str, str],
) -> None:
    container.clear()
    with container:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Solar dashboard").classes("text-2xl font-bold")
            ui.button(
                "Refresh",
                on_click=lambda: render_dashboard(container, database_path, timezone_name, pv_string_map),
                icon="refresh",
            )
        updated_at = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        ui.label(f"Last updated: {updated_at}").classes("text-sm text-gray-600")
        try:
            data = load_day(database_path, timezone_name, pv_string_map).copy()
        except Exception as error:
            ui.label(f"Unable to load database: {error}").classes("text-red-600")
            return

        ui.label("Hourly PV energy (kWh)").classes("text-lg font-semibold")
        actual_arrays = list(dict.fromkeys(pv_string_map.values()))
        actual_series = [
            {
                "name": f"Actual {array_name}",
                "type": "bar",
                "data": chart_values(dataframe_column(data, actual_column_name(array_name))),
            }
            for array_name in actual_arrays
        ]
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"data": ["Actual total", "Forecast total", *[series["name"] for series in actual_series]]},
            "xAxis": {"type": "category", "data": dataframe_column(data, "hour").tolist()},
            "yAxis": {"type": "value", "name": "kWh"},
            "series": [
                {"name": "Actual total", "type": "bar", "data": chart_values(dataframe_column(data, "solar"))},
                {"name": "Forecast total", "type": "bar", "data": chart_values(dataframe_column(data, "forecast_total"))},
                *actual_series,
            ],
        }).classes("w-full h-96")

        table_columns = [
            "hour",
            "solar",
            *[actual_column_name(array_name) for array_name in actual_arrays],
            "forecast_total",
            "load",
            "battery",
            "grid_import",
            "grid_export",
        ]
        table_labels = {
            "hour": "Hour",
            "solar": "Solar",
            **{actual_column_name(array_name): f"Actual {array_name}" for array_name in actual_arrays},
            "forecast_total": "Forecast total",
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


def load_day(database_path: str, timezone_name: str, pv_string_map: dict[str, str]) -> pd.DataFrame:
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            forecast_rows = load_forecast_for_day(session, day_start)
            actual = load_actual_hourly(session, day_start)
            actual_by_array = load_actual_arrays_hourly(session, day_start, pv_string_map)
    finally:
        engine.dispose()

    forecast = aggregate_forecast(forecast_rows, day_start)
    hourly = actual.merge(actual_by_array, on="hour", how="left").merge(forecast, on="hour", how="left").fillna(0.0)
    return hourly


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
    pv_string_map: dict[str, str],
) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = {actual_column_name(array_name) for array_name in pv_string_map.values()}
    if not pv_string_map:
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
        for pv_string in pv_string_map:
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
                for pv_string, array_name in pv_string_map.items():
                    before = previous_power.get(pv_string)
                    after = current_power[pv_string]
                    if before is not None and after is not None:
                        column = actual_column_name(array_name)
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


def aggregate_forecast(rows: list[ForecastSolarSample], day_start: datetime) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    points: list[dict[str, object]] = []
    for row in rows:
        try:
            timestamp = parse_time(row.forecast_time, timezone)
        except (TypeError, ValueError):
            continue
        for name, watt_hours in (("west", row.west_watt_hours), ("east", row.east_watt_hours)):
            if watt_hours is not None:
                points.append({"timestamp": timestamp, "array": name, "watt_hours": watt_hours})

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    if not points:
        return hours.assign(forecast_west=0.0, forecast_east=0.0, forecast_total=0.0)

    frame = pd.DataFrame(points).sort_values(["array", "timestamp"])
    frame["forecast_date"] = frame["timestamp"].dt.date
    frame["energy_kwh"] = (
        frame.groupby(["array", "forecast_date"])["watt_hours"]
        .diff()
        .fillna(frame["watt_hours"])
        .clip(lower=0)
        .div(1000)
    )
    frame["hour"] = frame["timestamp"].map(
        lambda value: format_hour(value - timedelta(microseconds=1), day_start)
    )
    frame = frame.dropna(subset=["hour"])
    grouped = frame.pivot_table(index="hour", columns="array", values="energy_kwh", aggfunc="sum", fill_value=0).reset_index()
    grouped = grouped.rename(columns={"west": "forecast_west", "east": "forecast_east"})
    for column in ("forecast_west", "forecast_east"):
        if column not in grouped:
            grouped[column] = 0.0
    grouped["forecast_total"] = grouped["forecast_west"] + grouped["forecast_east"]
    return hours.merge(grouped[["hour", "forecast_west", "forecast_east", "forecast_total"]], on="hour", how="left").fillna(0.0)


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


def actual_column_name(array_name: str) -> str:
    return f"actual_{re.sub(r'[^a-zA-Z0-9_]', '_', array_name).lower()}"


def chart_values(values: pd.Series) -> list[float | None]:
    return [None if pd.isna(value) else round(float(value), 3) for value in values]


def dataframe_column(frame: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, frame[name])


def dataframe_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = frame.round(3).to_dict(orient="records")
    return [{key: (None if pd.isna(value) else value) for key, value in row.items()} for row in rows]


if __name__ == "__main__":
    main()
