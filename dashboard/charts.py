from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
from nicegui import ui
from nicegui.elements.echart import EChart

from config import SolarArrayConfig

from .chart_display import ChartDataDisplay
from .data import actual_column_name, chart_values, dataframe_column, forecast_column_name
from .models import DashboardState, HistoricalCharts, HourlyCharts, PANEL_COLORS


SOLAR_COLOR = "#F9C74F"
LOAD_COLOR = "#0072B2"
BATTERY_COLOR = "#56B4E9"
INVERTER_COLOR = "#6C757D"
GRID_IMPORT_COLOR = "#D1495B"
GRID_EXPORT_COLOR = "#009E73"
NET_COST_COLOR = "#6C757D"
NO_SOLAR_COST_COLOR = "#7B2CBF"

ENERGY_TABLE_COLUMNS = (
    "period", "solar", "forecast_total", "load", "grid_import", "grid_export",
)


def analysis_column_labels(
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> dict[str, str]:
    labels = {
        "period": "Period",
        "time": "Time",
        "solar": "Solar",
        "forecast_total": "Solar Forecast",
        "load": "Load",
        "battery": "Battery",
        "inverter": "Inverter",
        "grid_import": "Grid Imported",
        "grid_export": "Grid Exported",
        "available_energy_kwh": "Available Energy",
        "soc_percent": "State of Charge",
        "grid_import_cost": "Import Cost",
        "grid_export_revenue": "Export Revenue",
        "net_cost": "Net Cost",
        "no_solar_battery_import_cost": "Net Cost if No Solar",
    }
    for array in forecast_arrays:
        labels[actual_column_name(array.panel_id)] = f"{array.name} Actual"
        labels[forecast_column_name(array.panel_id)] = f"{array.name} Forecast"
    return labels


def array_energy_table_columns(
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> tuple[str, ...]:
    return (
        "period",
        *(column for array in forecast_arrays for column in (
            actual_column_name(array.panel_id),
            forecast_column_name(array.panel_id),
        )),
    )


def render_hourly_charts(
    data: pd.DataFrame,
    power_hourly: pd.DataFrame,
    battery_hourly: pd.DataFrame,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    power_interval_minutes: int,
    zoom_start: float,
    zoom_end: float,
    on_time_zoom: Callable[[Any], None],
) -> HourlyCharts:
    energy_chart = render_energy_chart(data, "hour", "Average hourly energy (kWh)")

    power_title = ui.label(
        f"Average power by {time_group_label(power_interval_minutes)} (kW)"
    ).classes("text-lg font-semibold")
    ui.label(
        "Pinch with two fingers (or use the mouse wheel or range slider) to zoom. "
        "Detail changes automatically from hourly to 15-minute to one-minute data."
    ).classes("text-sm text-gray-600")
    power_chart = ui.echart(
        power_chart_options(
            power_hourly,
            forecast_arrays,
            actuals_to_forecast,
            zoom_start,
            zoom_end,
        )
    ).classes("w-full h-96")
    bind_time_zoom(power_chart, on_time_zoom)

    battery_title = ui.label(
        "Average battery by "
        f"{time_group_label(power_interval_minutes)}"
    ).classes("text-lg font-semibold mt-4")
    battery_chart = ui.echart(
        battery_chart_options(battery_hourly)
    ).classes("w-full h-96")

    array_energy_chart = render_array_energy_chart(
        data,
        "hour",
        "Average hourly energy by array (kWh)",
        forecast_arrays,
        actuals_to_forecast,
    )
    return HourlyCharts(
        energy=energy_chart,
        power_title=power_title,
        power=power_chart,
        battery_title=battery_title,
        battery=battery_chart,
        array_energy=array_energy_chart,
    )


def render_historical_charts(
    data: pd.DataFrame,
    power_data: pd.DataFrame,
    battery_data: pd.DataFrame,
    state: DashboardState,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    on_time_zoom: Callable[[Any], None],
) -> HistoricalCharts:
    column_labels = analysis_column_labels(forecast_arrays)
    energy_display = ChartDataDisplay(
        dataframe=data,
        title=lambda: f"Energy by {state.historical_frequency} (kWh)",
        render_chart=lambda: ui.echart(energy_chart_options(data, "period")).classes("w-full h-96"),
        chart_options=lambda dataframe: energy_chart_options(dataframe, "period"),
        column_labels=column_labels,
        table_columns=ENERGY_TABLE_COLUMNS,
        show_total=True,
    )
    power_display = ChartDataDisplay(
        dataframe=power_data,
        title=lambda: f"Average power by {time_group_label(state.historical_power_interval_minutes)} (kW)",
        render_chart=lambda: _render_historical_power_chart(
            power_data, forecast_arrays, actuals_to_forecast,
            state.historical_time_zoom_start, state.historical_time_zoom_end, on_time_zoom
        ),
        chart_options=lambda dataframe: power_chart_options(
            dataframe,
            forecast_arrays,
            actuals_to_forecast,
            state.historical_time_zoom_start,
            state.historical_time_zoom_end,
        ),
        column_labels=column_labels,
        show_total=True,
    )
    battery_display = ChartDataDisplay(
        dataframe=battery_data,
        title="Average battery by hour",
        render_chart=lambda: ui.echart(battery_chart_options(battery_data)).classes("w-full h-96"),
        chart_options=battery_chart_options,
        column_labels=column_labels,
        show_total=True,
    )
    array_energy_display = ChartDataDisplay(
        dataframe=data,
        title=lambda: f"Solar by array and {state.historical_frequency} (kWh)",
        render_chart=lambda: ui.echart(
            array_energy_chart_options(data, "period", forecast_arrays, actuals_to_forecast)
        ).classes("w-full h-96"),
        chart_options=lambda dataframe: array_energy_chart_options(
            dataframe, "period", forecast_arrays, actuals_to_forecast
        ),
        column_labels=column_labels,
        table_columns=array_energy_table_columns(forecast_arrays),
        show_total=True,
    )
    money_display = ChartDataDisplay(
        dataframe=money_dataframe(data),
        title=lambda: f"Costs by {state.historical_frequency}",
        render_chart=lambda: ui.echart(money_chart_options(data, "period")).classes("w-full h-96"),
        chart_options=lambda dataframe: money_chart_options(dataframe, "period"),
        column_labels=column_labels,
        show_total=True,
    )
    return HistoricalCharts(
        energy=energy_display,
        power=power_display,
        battery=battery_display,
        array_energy=array_energy_display,
        money=money_display,
    )


def _render_historical_power_chart(
    power_data: pd.DataFrame,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    zoom_start: float,
    zoom_end: float,
    on_time_zoom: Callable[[Any], None],
) -> EChart:
    chart = ui.echart(
        power_chart_options(
            power_data,
            forecast_arrays,
            actuals_to_forecast,
            zoom_start,
            zoom_end,
        )
    ).classes("w-full h-96")
    bind_time_zoom(chart, on_time_zoom)
    return chart


def render_energy_chart(data: pd.DataFrame, category_column: str, title: str) -> EChart:
    ui.label(title).classes("text-lg font-semibold mt-4")
    return ui.echart(energy_chart_options(data, category_column)).classes("w-full h-96")


def energy_chart_options(data: pd.DataFrame, category_column: str) -> dict[str, Any]:
    return {
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
            {
                "name": "Solar (Actual)",
                "type": "bar",
                "data": chart_values(dataframe_column(data, "solar")),
                "itemStyle": {"color": SOLAR_COLOR},
            },
            {
                "name": "Solar (Forecast)",
                "type": "bar",
                "data": chart_values(dataframe_column(data, "forecast_total")),
                "itemStyle": forecast_item_style(SOLAR_COLOR),
            },
            {"name": "Load", "type": "bar", "data": chart_values(dataframe_column(data, "load")), "itemStyle": {"color": LOAD_COLOR}},
            {"name": "Grid Imported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_import")), "itemStyle": {"color": GRID_IMPORT_COLOR}},
            {"name": "Grid Exported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_export")), "itemStyle": {"color": GRID_EXPORT_COLOR}},
        ],
    }


MONEY_SERIES = (
    ("Import Cost", "grid_import_cost", GRID_IMPORT_COLOR),
    ("Export Revenue", "grid_export_revenue", GRID_EXPORT_COLOR),
    ("Net Cost", "net_cost", NET_COST_COLOR),
    ("Net Cost if No Solar", "no_solar_battery_import_cost", NO_SOLAR_COST_COLOR),
)


def forecast_item_style(color: str) -> dict[str, Any]:
    """Distinguish a forecast without separating it from its actual series' colour."""
    return {
        "color": color,
        "decal": {
            "symbol": "rect",
            "symbolSize": 1,
            "color": "rgba(0, 0, 0, 0.35)",
            "backgroundColor": "rgba(0, 0, 0, 0)",
            "dashArrayX": [1, 0],
            "dashArrayY": [3, 4],
            "rotation": -0.7853981633974483,
        },
    }


def money_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    columns = ["period", *[column for _, column, _ in MONEY_SERIES]]
    return data.reindex(columns=columns).copy()


def money_chart_options(data: pd.DataFrame, category_column: str) -> dict[str, Any]:
    selected = {
        "Import Cost": True,
        "Export Revenue": True,
        "Net Cost": False,
        "Net Cost if No Solar": False,
    }
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {
            "data": [label for label, _, _ in MONEY_SERIES],
            "selected": selected,
        },
        "xAxis": {"type": "category", "data": dataframe_column(data, category_column).tolist()},
        "yAxis": {"type": "value", "name": "Currency"},
        "series": [
            {
                "name": label,
                "type": "bar",
                "data": chart_values(dataframe_column(data, column)),
                "itemStyle": {"color": color},
            }
            for label, column, color in MONEY_SERIES
        ],
    }


def render_array_energy_chart(
    data: pd.DataFrame,
    category_column: str,
    title: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> EChart:
    ui.label(title).classes("text-lg font-semibold mt-4")
    return ui.echart(
        array_energy_chart_options(
            data,
            category_column,
            forecast_arrays,
            actuals_to_forecast,
        )
    ).classes("w-full h-96")


def array_energy_chart_options(
    data: pd.DataFrame,
    category_column: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> dict[str, Any]:
    panel_names = {array.panel_id: array.name for array in forecast_arrays}
    forecast_panels = [array.panel_id for array in forecast_arrays]
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
                "itemStyle": forecast_item_style(panel_color),
            },
        ])
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [series["name"] for series in array_series]},
        "xAxis": {"type": "category", "data": dataframe_column(data, category_column).tolist()},
        "yAxis": {"type": "value", "name": "kWh"},
        "series": array_series,
    }


def power_chart_options(
    power_data: pd.DataFrame,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    zoom_start: float = 0.0,
    zoom_end: float = 100.0,
) -> dict[str, Any]:
    mapped_panels = set(actuals_to_forecast.values())
    power_series: list[tuple[str, str, str | None]] = [
        ("Solar", "solar", SOLAR_COLOR),
        *[
            (
                f"Solar - {array.name}",
                actual_column_name(array.panel_id),
                PANEL_COLORS[index % len(PANEL_COLORS)],
            )
            for index, array in enumerate(forecast_arrays)
            if array.panel_id in mapped_panels
        ],
        ("Load", "load", LOAD_COLOR),
        ("Battery", "battery", BATTERY_COLOR),
        ("Inverter", "inverter", INVERTER_COLOR),
        ("Grid Imported", "grid_import", GRID_IMPORT_COLOR),
        ("Grid Exported", "grid_export", GRID_EXPORT_COLOR),
    ]
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [label for label, _, _ in power_series]},
        "grid": time_chart_grid(),
        "dataZoom": time_data_zoom_options(zoom_start, zoom_end),
        "xAxis": {
            "type": "category",
            "data": dataframe_column(power_data, "time").tolist(),
        },
        "yAxis": {"type": "value", "name": "kW"},
        "series": [
            {
                "name": label,
                "type": "line",
                "showSymbol": False,
                "connectNulls": False,
                "data": chart_values(dataframe_column(power_data, column)),
                **(
                    {"lineStyle": {"color": color}, "itemStyle": {"color": color}}
                    if color is not None
                    else {}
                ),
            }
            for label, column, color in power_series
        ],
    }


def battery_chart_options(battery_data: pd.DataFrame) -> dict[str, Any]:
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Available energy", "State of charge"]},
        "grid": {"left": "3%", "right": "4%", "bottom": "8%", "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": dataframe_column(battery_data, "time").tolist(),
        },
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
                "lineStyle": {"color": BATTERY_COLOR},
                "itemStyle": {"color": BATTERY_COLOR},
                "data": chart_values(
                    dataframe_column(battery_data, "available_energy_kwh")
                ),
            },
            {
                "name": "State of charge",
                "type": "line",
                "showSymbol": False,
                "yAxisIndex": 1,
                "lineStyle": {"color": NO_SOLAR_COST_COLOR},
                "itemStyle": {"color": NO_SOLAR_COST_COLOR},
                "data": chart_values(dataframe_column(battery_data, "soc_percent")),
            },
        ],
    }


def time_chart_grid() -> dict[str, Any]:
    return {"left": "3%", "right": "4%", "bottom": 56, "containLabel": True}


def time_data_zoom_options(zoom_start: float, zoom_end: float) -> list[dict[str, Any]]:
    return [
        {
            "type": "inside",
            "xAxisIndex": 0,
            "start": zoom_start,
            "end": zoom_end,
            "filterMode": "none",
        },
        {
            "type": "slider",
            "xAxisIndex": 0,
            "start": zoom_start,
            "end": zoom_end,
            "bottom": 8,
            "height": 22,
            "filterMode": "none",
        },
    ]


def bind_time_zoom(chart: EChart, on_time_zoom: Callable[[Any], None]) -> None:
    chart.on(
        "chart:datazoom",
        on_time_zoom,
        args=["start", "end", "batch"],
        throttle=0.3,
    )


def data_zoom_range(event: Any) -> tuple[float, float] | None:
    args = getattr(event, "args", event)
    if isinstance(args, (list, tuple)) and len(args) == 1:
        args = args[0]
    if not isinstance(args, dict):
        return None
    values = args
    if values.get("start") is None or values.get("end") is None:
        batch = values.get("batch")
        if isinstance(batch, list) and batch and isinstance(batch[0], dict):
            values = batch[0]
    try:
        start = float(values["start"])
        end = float(values["end"])
    except (KeyError, TypeError, ValueError):
        return None
    start = min(100.0, max(0.0, start))
    end = min(100.0, max(0.0, end))
    return (start, end) if start < end else None


def power_interval_for_zoom(zoom_start: float, zoom_end: float) -> int:
    visible_minutes = (zoom_end - zoom_start) / 100 * 24 * 60
    if visible_minutes > 18 * 60:
        return 60
    if visible_minutes > 3 * 60:
        return 15
    return 1


def zoom_ranges_match(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return all(abs(left - right) < 0.01 for left, right in zip(first, second))


def time_group_label(minutes: int) -> str:
    if minutes == 60:
        return "hour"
    if minutes == 1:
        return "minute"
    return f"{minutes} minutes"


def update_chart(chart: EChart, options: dict[str, Any]) -> None:
    chart.options.clear()
    chart.options.update(options)
    chart.update()
