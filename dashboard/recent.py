from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui
from nicegui.elements.label import Label
from nicegui.elements.table import Table

from config import SolarArrayConfig

from . import data
from .live import LivePowerCollector
from .models import DashboardContext, DashboardElements, DashboardState, LivePowerData


class RecentTab:
    def __init__(
        self,
        context: DashboardContext,
        state: DashboardState,
        elements: DashboardElements,
        live_collector: LivePowerCollector,
    ) -> None:
        self.context = context
        self.state = state
        self.elements = elements
        self.live_collector = live_collector
        self.timezone = ZoneInfo(context.timezone_name)

    def render_recent_tab(self) -> None:
        try:
            day, telemetry, live_power, recent_daily, recent_monthly = self._load_data()
        except Exception as error:
            ui.label(f"Unable to load Recent data: {error}").classes("text-red-600")
            return


        self.elements.live_status_label = ui.label().classes("text-sm mt-1")
        self._update_live_status(live_power.collected_at_utc)

        latest = self._live_table_values(telemetry, live_power)
        self.elements.live_table = render_live_today_table(
            day,
            latest,
            self.context.forecast_arrays,
        )
        self.elements.battery_status_label = ui.label(
            data.battery_status_text(telemetry.latest)
        ).classes("text-sm font-medium mt-1")
        self.elements.recent_daily_table = render_energy_summary_table(
            recent_daily, "Past 7 days (kWh)", "Date"
        )
        self.elements.recent_monthly_table = render_energy_summary_table(
            recent_monthly, "Past 12 months (kWh)", "Month"
        )
        self.elements.live_timestamp = live_power.collected_at_utc

    def refresh_recent_tab(self) -> None:
        day, telemetry, live_power, recent_daily, recent_monthly = self._load_data()
        latest = self._live_table_values(telemetry, live_power)
        if self.elements.live_table is not None:
            self.elements.live_table.rows = data.summary_rows(
                day,
                latest,
                self.context.forecast_arrays,
            )
        if self.elements.battery_status_label is not None:
            self.elements.battery_status_label.set_text(data.battery_status_text(telemetry.latest))
        if self.elements.recent_daily_table is not None:
            self.elements.recent_daily_table.rows = data.energy_summary_rows(recent_daily)
        if self.elements.recent_monthly_table is not None:
            self.elements.recent_monthly_table.rows = data.energy_summary_rows(recent_monthly)
        self._update_live_status(live_power.collected_at_utc)
        self.elements.live_timestamp = live_power.collected_at_utc

    def refresh_live_power(self) -> None:
        table = self.elements.live_table
        if table is None:
            return
        live_power = self.live_collector.snapshot()
        if (
            live_power.collected_at_utc is not None
            and live_power.collected_at_utc != self.elements.live_timestamp
        ):
            rows: list[dict[str, Any]] = []
            for existing_row in table.rows:
                row = dict(existing_row)
                key = str(row.get("latest_key", ""))
                if key in live_power.values:
                    row["latest"] = data.format_dashboard_number(live_power.values[key])
                rows.append(row)
            table.rows = rows
            self.elements.live_timestamp = live_power.collected_at_utc
            if self.elements.updated_at_label is not None and live_power.collected_at_local:
                timestamp = data.parse_time(live_power.collected_at_local, self.timezone)
                self.elements.updated_at_label.set_text(
                    f"Last updated: {timestamp:%Y-%m-%d %H:%M:%S %Z}"
                )
        self._update_live_status(live_power.collected_at_utc)

    def _load_data(self) -> tuple[pd.DataFrame, data.TelemetryData, LivePowerData, pd.DataFrame, pd.DataFrame]:
        today = datetime.now(self.timezone).date()
        return (
            data.load_day(
                self.context.database_path,
                self.context.timezone_name,
                self.context.forecast_arrays,
                self.context.actuals_to_forecast,
            ).copy(),
            data.load_telemetry(
                self.context.database_path,
                self.context.timezone_name,
            ),
            self.live_collector.snapshot(),
            data.load_recent_daily_energy(
                self.context.database_path,
                self.context.timezone_name,
                today,
                7,
                self.context.forecast_arrays,
            ),
            data.load_recent_monthly_energy(
                self.context.database_path,
                self.context.timezone_name,
                today,
                12,
                self.context.forecast_arrays,
            ),
        )

    def _live_table_values(
        self,
        telemetry: data.TelemetryData,
        live_power: LivePowerData,
    ) -> dict[str, float | None]:
        values = {
            **live_power.values,
            "inverter_today": telemetry.latest["inverter_today"],
        }
        return values

    def _update_live_status(self, collected_at_utc: str | None) -> None:
        label = self.elements.live_status_label
        if label is None:
            return
        label.classes(replace="text-sm mt-1")
        if collected_at_utc is None:
            label.set_text("")
            label.set_visibility(False)
            return
        age_seconds = max(
            0.0,
            (datetime.now(ZoneInfo("UTC")) - data.parse_time(collected_at_utc, ZoneInfo("UTC"))).total_seconds(),
        )
        if age_seconds > 30:
            label.set_text(f"Warning: Live data is stale — last updated {data.human_readable_age(age_seconds)}.")
            label.set_visibility(True)
            label.classes(add="text-orange-700 font-semibold")
            return
        label.set_text("")
        label.set_visibility(False)


def render_live_today_table(
    dataframe: pd.DataFrame,
    latest: dict[str, float | None],
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> Table:
    ui.label("Live / Today").classes("text-base font-semibold")
    columns = [
        {"name": "metric", "label": "", "field": "metric", "align": "left"},
        {"name": "latest", "label": "Latest (kW)", "field": "latest", "align": "right"},
        {"name": "today", "label": "Today (kWh)", "field": "today", "align": "right"},
        {"name": "forecast", "label": "Forecast (kWh)", "field": "forecast", "align": "right"},
    ]
    return ui.table(
        columns=columns,
        rows=data.summary_rows(dataframe, latest, forecast_arrays),
        row_key="metric",
    ).props(
        "dense flat bordered"
    ).classes("max-w-full").style("width: fit-content")


def render_energy_summary_table(dataframe: pd.DataFrame, title: str, period_label: str) -> Table:
    ui.label(title).classes("text-base font-semibold mt-4")
    columns = [
        {"name": "period", "label": period_label, "field": "period", "align": "left"},
        {"name": "solar_actual", "label": "Solar Actual", "field": "solar_actual", "align": "right"},
        {"name": "solar_forecast", "label": "Solar Forecast", "field": "solar_forecast", "align": "right"},
        {"name": "load", "label": "Load", "field": "load", "align": "right"},
        {"name": "grid_import", "label": "Grid Import", "field": "grid_import", "align": "right"},
        {"name": "grid_export", "label": "Grid Export", "field": "grid_export", "align": "right"},
    ]
    return ui.table(columns=columns, rows=data.energy_summary_rows(dataframe), row_key="period").props(
        "dense flat bordered"
    ).classes("max-w-full").style("width: fit-content")
