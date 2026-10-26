from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

import pandas as pd
from nicegui import ui
from nicegui.elements.echart import EChart

from solar_model.config import SolarArrayConfig

from solar_model.dashboard.components.chart_display import ChartDataDisplay
from solar_model.dashboard.formatting import chart_values
from solar_model.storage.queries.dashboard import (
    IMPORT_COST_TARIFF_PREFIX,
    actual_column_name,
    dataframe_column,
    forecast_column_name,
)
from solar_model.dashboard.state import DashboardState, HistoricalCharts, PANEL_COLORS


SOLAR_COLOR = "#F9C74F"
LOAD_COLOR = "#0072B2"
BATTERY_COLOR = "#56B4E9"
INVERTER_COLOR = "#6C757D"
GRID_IMPORT_COLOR = "#D1495B"
GRID_EXPORT_COLOR = "#009E73"
NET_COST_COLOR = "#6C757D"
NO_SOLAR_COST_COLOR = "#7B2CBF"
CUSTOM_CHART_UNITS = {
    "solar": "kWh", "forecast_total": "kWh", "forecast_raw_total": "kWh",
    "load": "kWh", "house_load": "kWh", "battery": "kWh", "grid_import": "kWh", "grid_export": "kWh",
    "grid_import_cost": "€", "grid_export_revenue": "€", "net_cost": "€",
    "no_solar_battery_import_cost": "€", "import_rate": "€/kWh", "export_rate": "€/kWh",
}

CUSTOM_CHART_COLORS = (
    "#5470C6", "#91CC75", "#FAC858", "#EE6666", "#73C0DE",
    "#3BA272", "#FC8452", "#9A60B4", "#EA7CCC",
    "#2F4554", "#C23531", "#61A0A8",
)
CUSTOM_CHART_CROSS_PATH = "M-5,-3 L-3,-5 L0,-2 L3,-5 L5,-3 L2,0 L5,3 L3,5 L0,2 L-3,5 L-5,3 L-2,0 Z"
# Name, dash pattern, marker. Entries after these use ECharts defaults.
CUSTOM_CHART_STYLES = (
    ("Solid", "solid", None),
    ("Short dash", [5, 5], None),
    ("Dotted", [2, 6], None),
    ("Filled circle", "solid", "circle"),
    ("Filled square", "solid", "rect"),
    ("Filled triangle", "solid", "triangle"),
    ("Cross", "solid", "path://" + CUSTOM_CHART_CROSS_PATH),
    ("Empty circle", "solid", "emptyCircle"),
    ("Empty square", "solid", "emptyRect"),
    ("Empty triangle", "solid", "emptyTriangle"),
    ("Long dash", [14, 7], None),
)


def custom_chart_dimension_styles(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Assign styles to the full preview so filtering never changes them."""
    metrics = list(dict.fromkeys(str(item["metric"]) for item in series))
    splits = list(dict.fromkeys(
        str(item["split"]) for item in series if item.get("split") is not None
    ))
    color_dimension = "metrics" if len(metrics) >= len(splits) else "splits"
    result: dict[str, Any] = {"color_dimension": color_dimension}
    for dimension, entries in (("metrics", metrics), ("splits", splits)):
        result[dimension] = {}
        for index, entry in enumerate(entries):
            if dimension == color_dimension:
                result[dimension][entry] = {
                    "color": CUSTOM_CHART_COLORS[index % len(CUSTOM_CHART_COLORS)],
                }
            else:
                name, dash, symbol = (
                    CUSTOM_CHART_STYLES[index]
                    if index < len(CUSTOM_CHART_STYLES)
                    else ("Automatic", None, None)
                )
                result[dimension][entry] = {"name": name, "dash": dash, "symbol": symbol}
    return result


def custom_chart_legend_sample(style: dict[str, Any]) -> str:
    """Render the same colour, dash and marker used by a legend entry."""
    color = style.get("color", "#64748B")
    dash = style.get("dash")
    dash_attribute = (
        ' stroke-dasharray="' + " ".join(str(value) for value in dash) + '"'
        if isinstance(dash, list) else ""
    )
    symbol = style.get("symbol") or ""
    fill = "white" if symbol.startswith("empty") else color
    attributes = f'fill="{fill}" stroke="{color}" stroke-width="1.5"'
    marker = ""
    if symbol in {"circle", "emptyCircle"}:
        marker = f'<circle cx="30" cy="10" r="4" {attributes}/>'
    elif symbol in {"rect", "emptyRect"}:
        marker = f'<rect x="26" y="6" width="8" height="8" {attributes}/>'
    elif symbol in {"triangle", "emptyTriangle"}:
        marker = f'<polygon points="30,5 35,14 25,14" {attributes}/>'
    elif symbol.startswith("path://"):
        marker = f'<path d="{CUSTOM_CHART_CROSS_PATH}" transform="translate(30 10)" {attributes}/>'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="60" height="20" viewBox="0 0 60 20" aria-hidden="true">'
        f'<line x1="2" y1="10" x2="58" y2="10" stroke="{color}" stroke-width="2.5"{dash_attribute}/>'
        f'{marker}</svg>'
    )


def custom_chart_metric_unit(metric: str) -> str:
    if metric.startswith(("actual_panel_", "forecast_panel_")):
        return "kWh"
    return CUSTOM_CHART_UNITS.get(metric, "")

ENERGY_TABLE_COLUMNS = (
    "period", "solar", "forecast_total", "forecast_raw_total", "load", "house_load", "house_load_forecast", "grid_import", "grid_export",
)


def analysis_column_labels(
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> dict[str, str]:
    labels = {
        "period": "Period",
        "time": "Time",
        "solar": "Solar",
        "forecast_total": "Solar Forecast",
        "forecast_raw_total": "Solar (Forecast Raw)",
        "load": "Load",
        "house_load": "House Load",
        "house_load_forecast": "House Load (Forecast)",
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
        "import_rate": "Import Rate",
        "export_rate": "Export Rate",
    }
    for array in forecast_arrays:
        labels[actual_column_name(array.panel_id)] = f"{array.name} Actual"
        labels[forecast_column_name(array.panel_id)] = f"{array.name} Forecast"
    # Tariff band columns are added dynamically from stored sample metadata.
    return labels


def custom_chart_preview_options(
    preview: dict[str, Any],
    metric_labels: dict[str, str],
    *,
    visible_metrics: set[str] | None = None,
    visible_splits: set[str] | None = None,
) -> dict[str, Any]:
    """Build a live custom-builder preview from selected metrics and splits."""
    series = preview.get("series", [])
    if not series:
        return {
            "title": {
                "text": preview.get("empty_message", "Add a variable to begin"),
                "left": "center", "top": "middle",
                "textStyle": {"color": "#6B7280", "fontSize": 16},
            },
            "xAxis": {"type": "category", "data": []},
            "yAxis": {"type": "value"},
            "series": [],
        }
    dimension_styles = custom_chart_dimension_styles(series)
    chart_series = []
    for item in series:
        metric = str(item["metric"])
        split = None if item.get("split") is None else str(item["split"])
        if visible_metrics is not None and metric not in visible_metrics:
            continue
        if split is not None and visible_splits is not None and split not in visible_splits:
            continue
        unit = custom_chart_metric_unit(metric)
        metric_style = dimension_styles["metrics"][metric]
        split_style = dimension_styles["splits"].get(split, {})
        color_style = metric_style if dimension_styles["color_dimension"] == "metrics" else split_style
        line_style = split_style if dimension_styles["color_dimension"] == "metrics" else metric_style
        # An unsplit series always uses a solid line, even in a mixed preview.
        if split is None:
            metric_index = list(dimension_styles["metrics"]).index(metric)
            color_style = {"color": CUSTOM_CHART_COLORS[metric_index % len(CUSTOM_CHART_COLORS)]}
            line_style = {}
        appearance: dict[str, Any] = {
            "lineStyle": {"color": color_style["color"], "width": 2.5},
            "itemStyle": {"color": color_style["color"]},
            "showSymbol": False,
        }
        if line_style.get("dash") is not None:
            appearance["lineStyle"]["type"] = line_style["dash"]
        if line_style.get("symbol"):
            # Keep markers on actual data points, at most approximately twelve.
            valid_indices = [index for index, value in enumerate(item["data"]) if value is not None]
            step = max(1, (len(valid_indices) + 10) // 11)
            marker_indices = valid_indices[::step]
            if valid_indices and valid_indices[-1] not in marker_indices:
                marker_indices.append(valid_indices[-1])
            appearance.update({
                "showSymbol": True, "showAllSymbol": True, "symbolSize": 8,
                ":symbol": (
                    f"(value, params) => {json.dumps(marker_indices)}.includes(params.dataIndex)"
                    f" ? {json.dumps(line_style['symbol'])} : 'none'"
                ),
            })
        chart_series.append({
            "name": (
                metric_labels.get(metric, metric.replace("_", " ").title())
                + (f" ({unit})" if unit else "")
                + (f" — {split}" if split else "")
            ),
            "type": "line",
            **appearance,
            "data": item["data"],
        })
    return {
        "tooltip": {
            "trigger": "axis",
            ":valueFormatter": "value => Number(value).toFixed(1)",
        },
        "grid": {"left": 70, "right": 25, "top": 35, "bottom": 60},
        "xAxis": {"type": "category", "data": preview.get("periods", [])},
        "yAxis": {
            "type": "value",
            "name": _custom_chart_axis_unit(series),
            "axisLabel": {":formatter": "value => Number(value).toFixed(1)"},
        },
        "series": chart_series,
    }


def _custom_chart_axis_unit(series: list[dict[str, Any]]) -> str:
    units = {custom_chart_metric_unit(str(item["metric"])) for item in series}
    units.discard("")
    return units.pop() if len(units) == 1 else "Mixed units"


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
        show_series_selection=True,
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
        column_labels={**column_labels, "load": "House Load"},
        show_total=False,
        show_series_selection=True,
    )
    battery_display = ChartDataDisplay(
        dataframe=battery_data,
        title="Average battery by hour",
        render_chart=lambda: ui.echart(battery_chart_options(battery_data)).classes("w-full h-96"),
        chart_options=battery_chart_options,
        column_labels=column_labels,
        decimal_places={"available_energy_kwh": 1},
        show_total=False,
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
    money_data = money_dataframe(data)
    money_column_labels = {
        **column_labels,
        **{
            column: f"Import - {column.removeprefix(IMPORT_COST_TARIFF_PREFIX)}"
            for column in money_data.columns
            if column.startswith(IMPORT_COST_TARIFF_PREFIX)
        },
    }
    split_import_cost = {"value": True}
    selected_money_series: dict[str, bool] = {}

    def tariff_import_labels() -> list[str]:
        return [
            f"Import - {column.removeprefix(IMPORT_COST_TARIFF_PREFIX)}"
            for column in money_data.columns
            if column.startswith(IMPORT_COST_TARIFF_PREFIX)
        ]

    async def toggle_import_view(event) -> None:
        chart_options = await money_display.chart.run_chart_method("getOption")
        legend_options = chart_options.get("legend", []) if isinstance(chart_options, dict) else []
        if isinstance(legend_options, list) and legend_options and isinstance(legend_options[0], dict):
            selected = legend_options[0].get("selected", {})
            if isinstance(selected, dict):
                selected_money_series.clear()
                selected_money_series.update(selected)
        was_split = split_import_cost["value"]
        import_selected = (
            selected_money_series.get("Import Cost", True)
            if not was_split
            else any(selected_money_series.get(label, True) for label in tariff_import_labels())
        )
        split_import_cost["value"] = bool(event.value)
        if split_import_cost["value"]:
            for label in tariff_import_labels():
                selected_money_series[label] = import_selected
        else:
            selected_money_series["Import Cost"] = import_selected
        money_display.refresh_chart()

    money_display = ChartDataDisplay(
        dataframe=money_data,
        title=lambda: f"Costs by {state.historical_frequency}",
        render_chart=lambda: ui.echart(
            money_chart_options(money_data, "period", split_import_cost["value"], selected_money_series)
        ).classes("w-full h-96"),
        chart_options=lambda dataframe: money_chart_options(
            dataframe, "period", split_import_cost["value"], selected_money_series
        ),
        column_labels=money_column_labels,
        show_total=True,
        show_series_selection=True,
        total_aggregations={column: "mean" for _, column, _ in MONEY_RATE_SERIES},
        header_controls=lambda: ui.checkbox(
            "Split tariff",
            value=split_import_cost["value"],
            on_change=toggle_import_view,
        ).props("dense"),
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


def energy_chart_options(data: pd.DataFrame, category_column: str) -> dict[str, Any]:
    options = {
        "tooltip": {"trigger": "axis"},
        "grid": {"top": 125},
        "legend": {
            "data": [
                "Solar (Actual)",
                "Solar (Forecast)",
                "Solar (Forecast Raw)",
                "Load",
                "House Load",
                "Grid Imported",
                "Grid Exported",
            ],
            "selected": {"Solar (Forecast Raw)": False, "Load": False, "House Load": True},
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
            {
                "name": "Solar (Forecast Raw)",
                "type": "bar",
                "data": chart_values(dataframe_column(data, "forecast_raw_total")),
                "itemStyle": forecast_item_style("#6C757D"),
            },
            {"name": "Load", "type": "bar", "data": chart_values(dataframe_column(data, "load")), "itemStyle": {"color": "#6C757D"}},
            {"name": "House Load", "type": "bar", "data": chart_values(dataframe_column(data, "house_load")), "itemStyle": {"color": LOAD_COLOR}},
            {"name": "Grid Imported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_import")), "itemStyle": {"color": GRID_IMPORT_COLOR}},
            {"name": "Grid Exported", "type": "bar", "data": chart_values(dataframe_column(data, "grid_export")), "itemStyle": {"color": GRID_EXPORT_COLOR}},
        ],
    }

    if "house_load_forecast" in data:
        options["legend"]["data"].insert(4, "House Load (Forecast)")
        options["legend"]["selected"]["House Load (Forecast)"] = False
        options["series"].insert(4, {
            "name": "House Load (Forecast)", "type": "bar",
            "data": chart_values(data["house_load_forecast"]),
            "itemStyle": forecast_item_style(LOAD_COLOR),
        })
    return options


MONEY_SERIES = (
    ("Import Cost", "grid_import_cost", GRID_IMPORT_COLOR),
    ("Export Revenue", "grid_export_revenue", GRID_EXPORT_COLOR),
    ("Net Cost", "net_cost", NET_COST_COLOR),
    ("Net Cost if No Solar", "no_solar_battery_import_cost", NO_SOLAR_COST_COLOR),
)
MONEY_RATE_SERIES = (
    ("Import Rate", "import_rate", GRID_IMPORT_COLOR),
    ("Export Rate", "export_rate", GRID_EXPORT_COLOR),
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
    tariff_columns = sorted(column for column in data.columns if column.startswith(IMPORT_COST_TARIFF_PREFIX))
    columns = ["period", *[column for _, column, _ in MONEY_SERIES], *tariff_columns, *[column for _, column, _ in MONEY_RATE_SERIES]]
    result = data.reindex(columns=columns).copy()
    for _, column, _ in MONEY_RATE_SERIES:
        result[column] = result[column] * 100
    return result


def money_chart_options(
    data: pd.DataFrame,
    category_column: str,
    split_import_cost: bool = True,
    selected_series: dict[str, bool] | None = None,
) -> dict[str, Any]:
    tariff_columns = sorted(column for column in data.columns if column.startswith(IMPORT_COST_TARIFF_PREFIX))
    tariff_colors = (GRID_IMPORT_COLOR, "#E76F51", "#F4A261", "#2A9D8F", "#264653")
    import_cost_series = (
        [
            (f"Import - {column.removeprefix(IMPORT_COST_TARIFF_PREFIX)}", column, tariff_colors[index % len(tariff_colors)])
            for index, column in enumerate(tariff_columns)
        ]
        if split_import_cost and tariff_columns
        else [MONEY_SERIES[0]]
    )
    money_series = [*import_cost_series, *MONEY_SERIES[1:]]
    selected = {
        "Import Cost": True,
        **{label: True for label, _, _ in money_series},
        "Export Revenue": True,
        "Net Cost": False,
        "Net Cost if No Solar": False,
        "Import Rate": True,
        "Export Rate": True,
    }
    if selected_series:
        selected.update(selected_series)
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {
            "data": [label for label, _, _ in (*money_series, *MONEY_RATE_SERIES)],
            "selected": selected,
        },
        "xAxis": {"type": "category", "data": dataframe_column(data, category_column).tolist()},
        "yAxis": [
            {"type": "value", "name": "Currency"},
            {"type": "value", "name": "Rate"},
        ],
        "series": [
            {
                "name": label,
                "type": "bar",
                "data": chart_values(dataframe_column(data, column)),
                "itemStyle": {"color": color},
                **({"stack": "import-cost"} if column.startswith(IMPORT_COST_TARIFF_PREFIX) else {}),
            }
            for label, column, color in money_series
        ] + [
            {
                "name": label,
                "type": "line",
                "yAxisIndex": 1,
                "showSymbol": False,
                "step": "end",
                "lineStyle": {"color": color},
                "itemStyle": {"color": color},
                "data": chart_values(dataframe_column(data, column)),
            }
            for label, column, color in MONEY_RATE_SERIES
        ],
    }


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
        ("House Load", "load", LOAD_COLOR),
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
    estimated_capacity = estimated_battery_capacity_kwh(battery_data)
    energy_axis: dict[str, Any] = {
        "type": "value",
        "name": "kWh",
        "min": 0,
        "axisLabel": {":formatter": "value => Number(value).toFixed(1)"},
    }
    if estimated_capacity is not None:
        energy_axis["max"] = estimated_capacity
        energy_axis["interval"] = estimated_capacity / 5
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Available energy", "State of charge"]},
        "grid": {"left": "3%", "right": "4%", "bottom": "8%", "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": dataframe_column(battery_data, "time").tolist(),
        },
        "yAxis": [
            energy_axis,
            {"type": "value", "name": "%", "min": 0, "max": 100, "interval": 20},
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


def estimated_battery_capacity_kwh(battery_data: pd.DataFrame) -> float | None:
    """Estimate usable capacity from the displayed energy and state-of-charge data."""
    available_energy = pd.to_numeric(
        dataframe_column(battery_data, "available_energy_kwh"), errors="coerce"
    ).dropna()
    state_of_charge = pd.to_numeric(
        dataframe_column(battery_data, "soc_percent"), errors="coerce"
    ).dropna()
    average_soc = state_of_charge.mean()
    if available_energy.empty or pd.isna(average_soc) or average_soc <= 0:
        return None
    return float(available_energy.mean() * 100 / average_soc)


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
