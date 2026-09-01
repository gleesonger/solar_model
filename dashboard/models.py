from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import pandas as pd
from nicegui.elements.echart import EChart
from nicegui.elements.label import Label
from nicegui.elements.table import Table

from .chart_display import ChartDataDisplay

PANEL_COLORS = ("#5470c6", "#91cc75", "#fac858", "#ee6666")
SUMMARY_LATEST_KEYS = {
    "Solar": "solar",
    "Battery": "battery",
    "Inverter": "inverter",
    "Load": "load",
    "Grid-Imported": "grid_import",
    "Grid-Exported": "grid_export",
}
HOURLY_PERIODS = ("Today", "7 Days", "30 Days", "365 Days", "Lifetime")
HOURLY_PERIOD_DAYS = {
    "Today": 1,
    "7 Days": 7,
    "30 Days": 30,
    "365 Days": 365,
}


@dataclass(frozen=True)
class TelemetryData:
    power_15m: pd.DataFrame
    battery_15m: pd.DataFrame
    latest: dict[str, float | None]
    latest_collected_at_utc: str | None


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


@dataclass(frozen=True)
class HourlyCharts:
    energy: EChart
    power_title: Label
    power: EChart
    battery_title: Label
    battery: EChart
    array_energy: EChart


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
    live_status_label: Label | None = None
    updated_at_label: Label | None = None
    live_timestamp: str | None = None
    hourly_range_label: Label | None = None
    hourly_charts: HourlyCharts | None = None
    historical_range_label: Label | None = None
    historical_charts: HistoricalCharts | None = None
    system_table: Table | None = None


@dataclass
class DashboardState:
    hourly_start: date
    hourly_end: date
    historical_as_of: date
    hourly_period: str = "Today"
    hourly_dates_valid: bool = True
    power_interval_minutes: int = 15
    time_zoom_start: float = 0.0
    time_zoom_end: float = 100.0
    historical_count: int = 24
    historical_unit: str = "hours"
    historical_frequency: str = "hour"
    historical_power_interval_minutes: int = 60
    historical_time_zoom_start: float = 0.0
    historical_time_zoom_end: float = 100.0
    active_tab: str = "Recent"
