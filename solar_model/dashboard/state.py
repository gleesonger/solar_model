from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from nicegui.elements.label import Label
from nicegui.element import Element
from nicegui.elements.table import Table

from solar_model.dashboard.components.chart_display import ChartDataDisplay

PANEL_COLORS = ("#009E73", "#56B4E9", "#F9C74F", "#CC79A7")


@dataclass(frozen=True)
class HistoricalCharts:
    energy: ChartDataDisplay
    power: ChartDataDisplay
    battery: ChartDataDisplay
    array_energy: ChartDataDisplay
    money: ChartDataDisplay


@dataclass
class DashboardElements:
    live_table: Table | None = None
    battery_status_label: Label | None = None
    recent_daily_table: Table | None = None
    recent_monthly_table: Table | None = None
    live_update_indicator: Element | None = None
    live_update_message: Label | None = None
    live_timestamp: str | None = None
    historical_range_label: Label | None = None
    historical_charts: HistoricalCharts | None = None
    system_table: Table | None = None


@dataclass
class DashboardState:
    historical_as_of: date
    historical_count: int = 1
    historical_unit: str = "days"
    historical_frequency: str = "hour"
    historical_aggregation: str = "Sum"
    historical_power_interval_minutes: int = 60
    historical_time_zoom_start: float = 0.0
    historical_time_zoom_end: float = 100.0
    active_tab: str = "Recent"
