from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui
from nicegui.elements.column import Column
from nicegui.elements.label import Label
from nicegui.elements.table import Table
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from config import SolarArrayConfig, load_config
from database import ForecastSolarSample, SigenStorDevice, SigenStorLive, SigenStorModbusSample


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
SUMMARY_LATEST_KEYS = {
    "Solar": "solar",
    "Battery": "battery",
    "Inverter": "inverter",
    "Load": "load",
    "Grid-Imported": "grid_import",
    "Grid-Exported": "grid_export",
}


@dataclass(frozen=True)
class TelemetryData:
    power_15m: pd.DataFrame
    battery_15m: pd.DataFrame
    latest: dict[str, float | None]


@dataclass(frozen=True)
class HourlyRangeData:
    energy: pd.DataFrame
    power: pd.DataFrame
    battery: pd.DataFrame


@dataclass(frozen=True)
class LivePowerData:
    collected_at_utc: str | None
    collected_at_local: str | None
    values: dict[str, float | None]


@dataclass
class DashboardElements:
    live_table: Table | None = None
    live_status_label: Label | None = None
    updated_at_label: Label | None = None
    live_timestamp: str | None = None


@dataclass
class DashboardState:
    hourly_start: date
    hourly_end: date
    power_interval_minutes: int = 5
    historical_count: int = 14
    historical_unit: str = "days"
    historical_frequency: str = "day"
    active_tab: str = "Live/Today"


def main() -> None:
    config = load_config()
    database_path = config.database.path
    timezone_name = config.timezone
    forecast_arrays = config.forecast.arrays
    actuals_to_forecast = config.dashboard.actuals_to_forecast


    @ui.page("/")
    def page() -> None:
        today = datetime.now(ZoneInfo(timezone_name)).date()
        state = DashboardState(hourly_start=today, hourly_end=today)
        elements = DashboardElements()
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            render_dashboard(
                container,
                database_path,
                timezone_name,
                forecast_arrays,
                actuals_to_forecast,
                state,
                elements,
            )
        ui.timer(
            config.live.scheduler.interval_seconds,
            lambda: update_live_table(database_path, timezone_name, elements),
        )
        ui.timer(
            60,
            lambda: render_dashboard(
                container,
                database_path,
                timezone_name,
                forecast_arrays,
                actuals_to_forecast,
                state,
                elements,
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
    state: DashboardState,
    elements: DashboardElements,
) -> None:
    container.clear()
    with container:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Solar dashboard").classes("text-2xl font-bold")
            ui.button(
                "Refresh",
                on_click=lambda: render_dashboard(
                    container,
                    database_path,
                    timezone_name,
                    forecast_arrays,
                    actuals_to_forecast,
                    state,
                    elements,
                ),
                icon="refresh",
            )
        updated_at = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        elements.updated_at_label = ui.label(
            f"Last updated: {updated_at}"
        ).classes("text-sm text-gray-600")
        try:
            today = datetime.now(ZoneInfo(timezone_name)).date()
            historical_start = historical_start_date(
                today, state.historical_count, state.historical_unit
            )
            data = load_day(
                database_path, timezone_name, forecast_arrays, actuals_to_forecast
            ).copy()
            telemetry = load_telemetry(database_path, timezone_name)
            live_power = load_live_power(database_path)
            hourly_range = load_hourly_range(
                database_path,
                timezone_name,
                state.hourly_start,
                state.hourly_end,
                state.power_interval_minutes,
                forecast_arrays,
                actuals_to_forecast,
            )
            historical = load_historical_energy(
                database_path,
                timezone_name,
                historical_start,
                today,
                state.historical_frequency,
                forecast_arrays,
                actuals_to_forecast,
            )
            device_information = load_device_information(database_path)
        except Exception as error:
            ui.label(f"Unable to load database: {error}").classes("text-red-600")
            return

        with ui.tabs(
            on_change=lambda event: setattr(state, "active_tab", str(event.value)),
        ).classes("solar-tabs w-full") as tabs:
            live_today_tab = ui.tab("Live/Today")
            data_tab = ui.tab("Data by Hour")
            historical_tab = ui.tab("Historical")
            system_info_tab = ui.tab("System Info")
        selected_tab = {
            "Live/Today": live_today_tab,
            "Data by Hour": data_tab,
            "Historical": historical_tab,
            "System Info": system_info_tab,
        }.get(state.active_tab, live_today_tab)
        with ui.tab_panels(tabs, value=selected_tab).classes("w-full"):
            with ui.tab_panel(live_today_tab).classes("px-0"):
                summary_latest = {
                    **live_power.values,
                    "inverter_today": telemetry.latest["inverter_today"],
                }
                elements.live_table = render_live_today(data, summary_latest)
                elements.live_status_label = ui.label().classes("text-sm mt-1")
                update_live_status(
                    elements.live_status_label,
                    live_power.collected_at_utc,
                )
                elements.live_timestamp = live_power.collected_at_utc
            with ui.tab_panel(data_tab).classes("px-0"):
                with ui.row().classes("w-full items-end gap-3"):
                    start_input = ui.input(
                        "First date", value=state.hourly_start.isoformat()
                    ).props("type=date outlined dense")
                    end_input = ui.input(
                        "End date", value=state.hourly_end.isoformat()
                    ).props("type=date outlined dense")

                    def apply_date_range() -> None:
                        try:
                            start = date.fromisoformat(str(start_input.value))
                            end = date.fromisoformat(str(end_input.value))
                        except ValueError:
                            ui.notify("Enter valid first and end dates", type="negative")
                            return
                        if start > end:
                            ui.notify("First date must not be after end date", type="negative")
                            return
                        state.hourly_start = start
                        state.hourly_end = end
                        state.active_tab = "Data by Hour"
                        render_dashboard(
                            container,
                            database_path,
                            timezone_name,
                            forecast_arrays,
                            actuals_to_forecast,
                            state,
                            elements,
                        )

                    ui.button("Apply", on_click=apply_date_range, icon="date_range")
                ui.label(
                    f"Hourly averages from {state.hourly_start:%d %b %Y} "
                    f"to {state.hourly_end:%d %b %Y}, inclusive"
                ).classes("text-sm text-gray-600 mb-2")

                def apply_power_interval(minutes: int) -> None:
                    state.power_interval_minutes = minutes
                    state.active_tab = "Data by Hour"
                    render_dashboard(
                        container,
                        database_path,
                        timezone_name,
                        forecast_arrays,
                        actuals_to_forecast,
                        state,
                        elements,
                    )

                render_data_by_hour(
                    hourly_range.energy,
                    hourly_range.power,
                    hourly_range.battery,
                    forecast_arrays,
                    actuals_to_forecast,
                    state.power_interval_minutes,
                    apply_power_interval,
                )
            with ui.tab_panel(historical_tab).classes("px-0"):
                with ui.row().classes("w-full items-end gap-3"):
                    historical_count_input = ui.number(
                        "Last", value=state.historical_count, min=1, step=1
                    ).props("outlined dense").classes("w-28")
                    historical_unit_input = ui.select(
                        ["days", "weeks", "months", "years"],
                        value=state.historical_unit,
                        label="Period",
                    ).props("outlined dense").classes("w-36")
                    historical_frequency_input = ui.select(
                        ["day", "week", "month", "season", "year"],
                        value=state.historical_frequency,
                        label="Group by",
                    ).props("outlined dense").classes("w-36")

                    def apply_historical_range() -> None:
                        try:
                            raw_count = float(historical_count_input.value)
                            count = int(raw_count)
                        except (TypeError, ValueError):
                            ui.notify("Last must be a positive whole number", type="negative")
                            return
                        if count < 1 or raw_count != count:
                            ui.notify("Last must be a positive whole number", type="negative")
                            return
                        unit = str(historical_unit_input.value)
                        frequency = str(historical_frequency_input.value)
                        if unit not in {"days", "weeks", "months", "years"}:
                            ui.notify("Select a valid period", type="negative")
                            return
                        if frequency not in {"day", "week", "month", "season", "year"}:
                            ui.notify("Select a valid grouping", type="negative")
                            return
                        state.historical_count = count
                        state.historical_unit = unit
                        state.historical_frequency = frequency
                        state.active_tab = "Historical"
                        render_dashboard(
                            container,
                            database_path,
                            timezone_name,
                            forecast_arrays,
                            actuals_to_forecast,
                            state,
                            elements,
                        )

                    ui.button("Apply", on_click=apply_historical_range, icon="date_range")
                ui.label(
                    f"Total energy grouped by {state.historical_frequency}: "
                    f"{historical_start:%d %b %Y} to {today:%d %b %Y}, inclusive"
                ).classes("text-sm text-gray-600 mb-2")
                render_historical(
                    historical,
                    state.historical_frequency,
                    forecast_arrays,
                    actuals_to_forecast,
                )
            with ui.tab_panel(system_info_tab).classes("px-0"):
                render_system_information(device_information)


def render_system_information(device_information: list[dict[str, str]]) -> None:
    ui.label("System information").classes("text-lg font-semibold")
    ui.table(
        columns=[
            {"name": "variable", "label": "Variable", "field": "variable", "align": "left"},
            {"name": "value", "label": "Value", "field": "value", "align": "right"},
            {"name": "unit", "label": "Unit", "field": "unit", "align": "left"},
        ],
        rows=device_information,
        row_key="variable",
    ).props("dense flat bordered").classes("w-full max-w-3xl")


def render_live_today(data: pd.DataFrame, latest: dict[str, float | None]) -> Table:
    summary_columns = [
        {"name": "metric", "label": "", "field": "metric", "align": "left"},
        {"name": "latest", "label": "Latest (kW)", "field": "latest", "align": "right"},
        {"name": "today", "label": "Today (kWh)", "field": "today", "align": "right"},
        {"name": "forecast", "label": "Forecast (kWh)", "field": "forecast", "align": "right"},
    ]
    return ui.table(
        columns=summary_columns,
        rows=summary_rows(data, latest),
        row_key="metric",
    ).props("dense flat bordered").classes("w-full max-w-3xl")


def render_data_by_hour(
    data: pd.DataFrame,
    power_hourly: pd.DataFrame,
    battery_hourly: pd.DataFrame,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
    power_interval_minutes: int,
    on_power_interval_change: Callable[[int], None],
) -> None:
    render_energy_chart(data, "hour", "Average hourly energy (kWh)")

    with ui.row().classes("w-full items-end gap-3 mt-4"):
        power_interval_input = ui.number(
            "Average over (minutes)",
            value=power_interval_minutes,
            min=1,
            step=1,
        ).props("outlined dense").classes("w-48")

        def apply_interval() -> None:
            try:
                raw_minutes = float(power_interval_input.value)
                minutes = int(raw_minutes)
            except (TypeError, ValueError):
                ui.notify("Power interval must be a positive whole number", type="negative")
                return
            if minutes < 1 or raw_minutes != minutes:
                ui.notify("Power interval must be a positive whole number", type="negative")
                return
            on_power_interval_change(minutes)

        ui.button("Apply", on_click=apply_interval, icon="schedule")
    minute_label = "minute" if power_interval_minutes == 1 else "minutes"
    ui.label(
        f"Average power over {power_interval_minutes} {minute_label} (kW)"
    ).classes("text-lg font-semibold")
    mapped_panels = set(actuals_to_forecast.values())
    power_series: list[tuple[str, str, str | None]] = [
        ("Solar", "solar", None),
        *[
            (
                f"Solar - {array.name}",
                actual_column_name(array.panel),
                PANEL_COLORS[index % len(PANEL_COLORS)],
            )
            for index, array in enumerate(forecast_arrays)
            if array.panel in mapped_panels
        ],
        ("Load", "load", None),
        ("Battery", "battery", None),
        ("Inverter", "inverter", None),
        ("Grid Imported", "grid_import", None),
        ("Grid Exported", "grid_export", None),
    ]
    ui.echart({
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [label for label, _, _ in power_series]},
        "xAxis": {"type": "category", "data": dataframe_column(power_hourly, "time").tolist()},
        "yAxis": {"type": "value", "name": "kW"},
        "series": [
            {
                "name": label,
                "type": "line",
                "showSymbol": False,
                "connectNulls": False,
                "data": chart_values(dataframe_column(power_hourly, column)),
                **(
                    {"lineStyle": {"color": color}, "itemStyle": {"color": color}}
                    if color is not None
                    else {}
                ),
            }
            for label, column, color in power_series
        ],
    }).classes("w-full h-96")

    ui.label("Average battery energy and state of charge by hour").classes("text-lg font-semibold mt-4")
    ui.echart({
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Available energy", "State of charge"]},
        "xAxis": {"type": "category", "data": dataframe_column(battery_hourly, "time").tolist()},
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
                "data": chart_values(dataframe_column(battery_hourly, "available_energy_kwh")),
            },
            {
                "name": "State of charge",
                "type": "line",
                "showSymbol": False,
                "yAxisIndex": 1,
                "data": chart_values(dataframe_column(battery_hourly, "soc_percent")),
            },
        ],
    }).classes("w-full h-96")

    render_array_energy_chart(
        data,
        "hour",
        "Average hourly solar energy by array (kWh)",
        forecast_arrays,
        actuals_to_forecast,
    )


def render_historical(
    data: pd.DataFrame,
    frequency: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> None:
    render_energy_chart(data, "period", f"Energy by {frequency} (kWh)")
    render_array_energy_chart(
        data,
        "period",
        f"Solar energy by array and {frequency} (kWh)",
        forecast_arrays,
        actuals_to_forecast,
    )


def render_energy_chart(data: pd.DataFrame, category_column: str, title: str) -> None:
    ui.label(title).classes("text-lg font-semibold mt-4")
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
        "xAxis": {"type": "category", "data": dataframe_column(data, category_column).tolist()},
        "yAxis": {"type": "value", "name": "kWh"},
        "series": [
            {"name": "Solar (Actual)", "type": "bar", "data": chart_values(dataframe_column(data, "solar"))},
            {"name": "Solar (Forecast)", "type": "bar", "data": chart_values(dataframe_column(data, "forecast_total"))},
            {"name": "Load", "type": "bar", "data": chart_values(dataframe_column(data, "load"))},
            {"name": "Grid Imported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_import"))},
            {"name": "Grid Exported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_export"))},
        ],
    }).classes("w-full h-96")


def render_array_energy_chart(
    data: pd.DataFrame,
    category_column: str,
    title: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> None:
    ui.label(title).classes("text-lg font-semibold mt-4")
    panel_names = {array.panel: array.name for array in forecast_arrays}
    forecast_panels = [array.panel for array in forecast_arrays]
    mapped_panels = set(actuals_to_forecast.values())
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
        "xAxis": {"type": "category", "data": dataframe_column(data, category_column).tolist()},
        "yAxis": {"type": "value", "name": "kWh"},
        "series": array_series,
    }).classes("w-full h-96")


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


def load_live_power(database_path: str) -> LivePowerData:
    empty_values: dict[str, float | None] = {
        key: None for key in SUMMARY_LATEST_KEYS.values()
    }
    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        try:
            with Session(engine) as session:
                row = session.scalar(select(SigenStorLive).limit(1))
        except OperationalError:
            row = None
    finally:
        engine.dispose()

    if row is None:
        return LivePowerData(None, None, empty_values)
    grid_power = row.plant_grid_power_kw
    return LivePowerData(
        collected_at_utc=row.collected_at_utc,
        collected_at_local=row.collected_at_local,
        values={
            "solar": row.plant_pv_power_kw,
            "battery": row.plant_battery_power_kw,
            "inverter": row.inverter_power_kw,
            "load": row.plant_load_power_kw,
            "grid_import": max(grid_power, 0.0) if grid_power is not None else None,
            "grid_export": max(-grid_power, 0.0) if grid_power is not None else None,
        },
    )


def update_live_table(
    database_path: str,
    timezone_name: str,
    elements: DashboardElements,
) -> None:
    table = elements.live_table
    if table is None:
        return
    live_power = load_live_power(database_path)
    if (
        live_power.collected_at_utc is not None
        and live_power.collected_at_utc != elements.live_timestamp
    ):
        rows: list[dict[str, Any]] = []
        for existing_row in table.rows:
            row = dict(existing_row)
            key = SUMMARY_LATEST_KEYS.get(str(row.get("metric")))
            if key is not None:
                row["latest"] = format_dashboard_number(live_power.values[key])
            rows.append(row)
        table.rows = rows
        elements.live_timestamp = live_power.collected_at_utc
        if elements.updated_at_label is not None and live_power.collected_at_local is not None:
            updated_at = parse_time(
                live_power.collected_at_local, ZoneInfo(timezone_name)
            ).strftime("%Y-%m-%d %H:%M:%S %Z")
            elements.updated_at_label.set_text(f"Last updated: {updated_at}")

    if elements.live_status_label is not None:
        update_live_status(
            elements.live_status_label,
            live_power.collected_at_utc,
        )


def update_live_status(
    label: Label,
    collected_at_utc: str | None,
) -> None:
    label.classes(replace="text-sm mt-1")
    if collected_at_utc is None:
        label.set_visibility(True)
        label.set_text("Warning: No live data has been received.")
        label.classes(add="text-orange-700 font-semibold")
        return

    utc = ZoneInfo("UTC")
    collected_at = parse_time(collected_at_utc, utc)
    age_seconds = max(
        0.0,
        (datetime.now(utc) - collected_at).total_seconds(),
    )
    elapsed = human_readable_age(age_seconds)
    if age_seconds > 30:
        label.set_visibility(True)
        label.set_text(f"Warning: Live data is stale — last updated {elapsed}.")
        label.classes(add="text-orange-700 font-semibold")
    else:
        label.set_text("")
        label.set_visibility(False)


def human_readable_age(age_seconds: float) -> str:
    seconds = max(0, int(age_seconds))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"
    if seconds < 3_600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86_400:
        hours = seconds // 3_600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86_400
    return f"{days} day{'s' if days != 1 else ''} ago"


def load_hourly_range(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    power_interval_minutes: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> HourlyRangeData:
    if end_date < start_date:
        raise ValueError("End date must not be before first date")
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")

    timezone = ZoneInfo(timezone_name)
    daily_energy: list[pd.DataFrame] = []
    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            selected_samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    >= start_date.isoformat()
                )
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    <= end_date.isoformat()
                )
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
            daily_energy = load_daily_energy_frames(
                session,
                timezone,
                start_date,
                end_date,
                forecast_arrays,
                actuals_to_forecast,
            )
    finally:
        engine.dispose()

    energy = average_daily_hourly_energy(daily_energy)
    power, battery = average_telemetry_by_interval(
        selected_samples,
        timezone,
        power_interval_minutes,
        actuals_to_forecast,
    )
    return HourlyRangeData(energy=energy, power=power, battery=battery)


def load_historical_energy(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    frequency: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> pd.DataFrame:
    if end_date < start_date:
        raise ValueError("End date must not be before first date")
    if frequency not in {"day", "week", "month", "season", "year"}:
        raise ValueError(f"Unsupported historical frequency: {frequency}")

    timezone = ZoneInfo(timezone_name)
    engine = create_engine(f"sqlite:///{Path(database_path)}", future=True)
    try:
        with Session(engine) as session:
            daily_frames = load_daily_energy_frames(
                session,
                timezone,
                start_date,
                end_date,
                forecast_arrays,
                actuals_to_forecast,
            )
    finally:
        engine.dispose()

    combined = pd.concat(daily_frames, ignore_index=True)
    value_columns = [column for column in combined.columns if column not in {"date", "hour"}]
    for column in value_columns:
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    combined["bucket_start"] = dataframe_column(combined, "date").map(
        lambda value: historical_bucket_start(date.fromisoformat(str(value)), frequency)
    )
    grouped = cast(
        pd.DataFrame,
        combined.groupby("bucket_start", as_index=False)[value_columns].sum(min_count=1),
    )
    grouped["period"] = dataframe_column(grouped, "bucket_start").map(
        lambda value: historical_bucket_label(value, frequency)
    )
    return cast(pd.DataFrame, grouped[["period", *value_columns]])


def load_daily_energy_frames(
    session: Session,
    timezone: tzinfo,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, str],
) -> list[pd.DataFrame]:
    daily_energy: list[pd.DataFrame] = []
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    actual_dates = set(session.scalars(
        select(func.substr(SigenStorModbusSample.collected_at_local, 1, 10))
        .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) >= start_text)
        .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) <= end_text)
        .distinct()
    ).all())
    forecast_dates = set(session.scalars(
        select(func.substr(ForecastSolarSample.collected_at_local, 1, 10))
        .where(func.substr(ForecastSolarSample.collected_at_local, 1, 10) >= start_text)
        .where(func.substr(ForecastSolarSample.collected_at_local, 1, 10) <= end_text)
        .distinct()
    ).all())
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = [actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()]
    number_of_days = (end_date - start_date).days + 1
    for day_offset in range(number_of_days):
        selected_date = start_date + timedelta(days=day_offset)
        selected_date_text = selected_date.isoformat()
        day_start = datetime(
            selected_date.year,
            selected_date.month,
            selected_date.day,
            tzinfo=timezone,
        )
        if selected_date_text in actual_dates:
            actual = load_actual_hourly(session, day_start, fill_missing=False)
            actual_by_array = load_actual_arrays_hourly(
                session,
                day_start,
                actuals_to_forecast,
                fill_missing=False,
            )
        else:
            actual = hours.assign(
                solar=float("nan"),
                load=float("nan"),
                battery=float("nan"),
                grid_import=float("nan"),
                grid_export=float("nan"),
            )
            actual_by_array = hours.assign(
                **{column: float("nan") for column in actual_columns}
            )
        forecast_rows = (
            load_forecast_for_day(session, day_start)
            if selected_date_text in forecast_dates
            else []
        )
        forecast = aggregate_forecast(
            forecast_rows, day_start, forecast_arrays, fill_missing=False
        )
        day = actual.merge(actual_by_array, on="hour", how="left").merge(
            forecast, on="hour", how="left"
        )
        day["date"] = selected_date.isoformat()
        daily_energy.append(day)
    return daily_energy


def historical_start_date(end_date: date, count: int, unit: str) -> date:
    if count < 1:
        raise ValueError("Historical period must be a positive whole number")
    if unit == "days":
        return end_date - timedelta(days=count - 1)
    if unit == "weeks":
        return end_date - timedelta(weeks=count) + timedelta(days=1)
    if unit == "months":
        return shift_date_by_months(end_date, -count) + timedelta(days=1)
    if unit == "years":
        return shift_date_by_months(end_date, -12 * count) + timedelta(days=1)
    raise ValueError(f"Unsupported historical period: {unit}")


def shift_date_by_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    following_month = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (following_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def historical_bucket_start(value: date, frequency: str) -> date:
    if frequency == "day":
        return value
    if frequency == "week":
        return value - timedelta(days=value.weekday())
    if frequency == "month":
        return value.replace(day=1)
    if frequency == "season":
        if value.month in {12, 1, 2}:
            winter_year = value.year if value.month == 12 else value.year - 1
            return date(winter_year, 12, 1)
        season_month = 3 if value.month <= 5 else 6 if value.month <= 8 else 9
        return date(value.year, season_month, 1)
    if frequency == "year":
        return date(value.year, 1, 1)
    raise ValueError(f"Unsupported historical frequency: {frequency}")


def historical_bucket_label(value: date, frequency: str) -> str:
    if frequency == "day":
        return value.isoformat()
    if frequency == "week":
        return f"Week of {value:%d %b %Y}"
    if frequency == "month":
        return value.strftime("%b %Y")
    if frequency == "season":
        season = {12: "Winter", 3: "Spring", 6: "Summer", 9: "Autumn"}[value.month]
        if value.month == 12:
            return f"{season} {value.year}/{str(value.year + 1)[-2:]}"
        return f"{season} {value.year}"
    if frequency == "year":
        return str(value.year)
    raise ValueError(f"Unsupported historical frequency: {frequency}")


def load_telemetry(
    database_path: str,
    timezone_name: str,
) -> TelemetryData:
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
        for name in ("available_energy_kwh", "soc_percent"):
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
    return TelemetryData(
        power_15m=pd.DataFrame(power_rows),
        battery_15m=pd.DataFrame(battery_rows),
        latest=latest,
    )


def average_daily_hourly_energy(daily_frames: list[pd.DataFrame]) -> pd.DataFrame:
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    if not daily_frames:
        return hours

    combined = pd.concat(daily_frames, ignore_index=True)
    value_columns = [column for column in combined.columns if column not in {"date", "hour"}]
    for column in value_columns:
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    averaged = cast(pd.DataFrame, combined.groupby("hour", as_index=False)[value_columns].mean())
    return hours.merge(averaged, on="hour", how="left")


def average_telemetry_by_interval(
    samples: list[SigenStorModbusSample],
    timezone: tzinfo,
    power_interval_minutes: int,
    actuals_to_forecast: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")
    array_power_names = tuple(
        actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()
    )
    power_names = (
        "solar",
        *array_power_names,
        "load",
        "battery",
        "inverter",
        "grid_import",
        "grid_export",
    )
    battery_names = ("available_energy_kwh", "soc_percent")
    daily_totals: dict[tuple[str, str, str], float] = {}
    daily_counts: dict[tuple[str, str, str], int] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone)
        day = timestamp.date().isoformat()
        power_bucket = interval_label(timestamp, power_interval_minutes)
        battery_bucket = f"{timestamp.hour:02d}:00"
        grid_power = sample.plant_grid_power_kw
        values: dict[str, float | None] = {
            "solar": sample.plant_pv_power_kw,
            "load": sample.plant_load_power_kw,
            "battery": sample.plant_battery_power_kw,
            "inverter": sample.inverter_power_kw,
            "grid_import": max(grid_power, 0.0) if grid_power is not None else None,
            "grid_export": max(-grid_power, 0.0) if grid_power is not None else None,
            "available_energy_kwh": sample.inverter_battery_available_discharge_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for pv_string, panel_id in actuals_to_forecast.items():
            voltage, current = PV_STRING_READERS[pv_string](sample)
            values[actual_column_name(panel_id)] = (
                max(voltage * current, 0.0) / 1000
                if voltage is not None and current is not None
                else None
            )
        for name, value in values.items():
            if value is None:
                continue
            bucket = battery_bucket if name in battery_names else power_bucket
            key = (day, bucket, name)
            daily_totals[key] = daily_totals.get(key, 0.0) + value
            daily_counts[key] = daily_counts.get(key, 0) + 1

    hourly_totals: dict[tuple[str, str], float] = {}
    hourly_counts: dict[tuple[str, str], int] = {}
    for daily_key, total in daily_totals.items():
        _, hour, name = daily_key
        key = (hour, name)
        daily_average = total / daily_counts[daily_key]
        hourly_totals[key] = hourly_totals.get(key, 0.0) + daily_average
        hourly_counts[key] = hourly_counts.get(key, 0) + 1

    def build_frame(names: tuple[str, ...], bucket_labels: list[str]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for bucket in bucket_labels:
            row: dict[str, Any] = {"time": bucket}
            for name in names:
                key = (bucket, name)
                count = hourly_counts.get(key, 0)
                row[name] = hourly_totals.get(key, 0.0) / count if count else None
            rows.append(row)
        return pd.DataFrame(rows)

    power_buckets = [
        minute_of_day_label(minute)
        for minute in range(0, 24 * 60, power_interval_minutes)
    ]
    battery_buckets = [f"{hour:02d}:00" for hour in range(24)]
    return build_frame(power_names, power_buckets), build_frame(battery_names, battery_buckets)


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


def load_actual_hourly(
    session: Session,
    day_start: datetime,
    *,
    fill_missing: bool = True,
) -> pd.DataFrame:
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
        missing_value = 0.0 if fill_missing else float("nan")
        return hours.assign(
            solar=missing_value,
            load=missing_value,
            battery=missing_value,
            grid_import=missing_value,
            grid_export=missing_value,
        )

    grouped["hour"] = grouped["hour_key"].map(lambda value: f"{int(value):02d}:00")
    grouped["battery"] = pd.concat(
        [grouped["battery_charge"], -grouped["battery_discharge"]], axis=1
    ).sum(axis=1, min_count=1)
    grouped = grouped[["hour", "solar", "load", "battery", "grid_import", "grid_export"]]
    merged = hours.merge(grouped, on="hour", how="left")
    return merged.fillna(0.0) if fill_missing else merged


def load_actual_arrays_hourly(
    session: Session,
    day_start: datetime,
    actuals_to_forecast: dict[str, str],
    *,
    fill_missing: bool = True,
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
        missing_value = 0.0 if fill_missing else float("nan")
        hours[column] = dataframe_column(hours, "hour").map(
            lambda hour, column=column: totals.get((hour, column), missing_value)
        )
    return hours


def aggregate_forecast(
    rows: list[ForecastSolarSample],
    day_start: datetime,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    *,
    fill_missing: bool = True,
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
        missing_value = 0.0 if fill_missing else float("nan")
        return hours.assign(
            **{column: missing_value for column in [*forecast_columns, "forecast_total"]}
        )

    frame = pd.DataFrame(points).sort_values(["panel_id", "timestamp"])
    available_panels = set(dataframe_column(frame, "panel_id"))
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
    for array in forecast_arrays:
        column = forecast_column_name(array.panel)
        if column not in grouped:
            grouped[column] = float("nan")
    result = hours.merge(grouped[["hour", *forecast_columns]], on="hour", how="left")
    for array in forecast_arrays:
        column = forecast_column_name(array.panel)
        if array.panel in available_panels or fill_missing:
            result[column] = dataframe_column(result, column).fillna(0.0)
    result["forecast_total"] = result[forecast_columns].sum(axis=1, min_count=1)
    return result


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


def interval_label(value: datetime, interval_minutes: int) -> str:
    minute_of_day = value.hour * 60 + value.minute
    bucket_start = minute_of_day // interval_minutes * interval_minutes
    return minute_of_day_label(bucket_start)


def minute_of_day_label(minute_of_day: int) -> str:
    hour, minute = divmod(minute_of_day, 60)
    return f"{hour:02d}:{minute:02d}"


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
    return [
        {
            "metric": label,
            "latest": format_dashboard_number(latest[key]),
            "today": format_dashboard_number(today[key]),
            "forecast": format_dashboard_number(forecast[key]),
        }
        for label, key in SUMMARY_LATEST_KEYS.items()
    ]


def format_dashboard_number(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    rounded = round(float(value), 1)
    return f"{0.0 if rounded == 0 else rounded:.1f}"


def chart_values(values: pd.Series) -> list[float | None]:
    rounded_values: list[float | None] = []
    for value in values:
        if pd.isna(value):
            rounded_values.append(None)
            continue
        rounded = round(float(value), 1)
        rounded_values.append(0.0 if rounded == 0 else rounded)
    return rounded_values


def dataframe_column(frame: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, frame[name])


if __name__ == "__main__":
    main()
