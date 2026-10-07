from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui
from nicegui.elements.label import Label
from nicegui.elements.table import Table

from common import LOGGER
from config import Config

from . import data
from .live import LivePowerCollector
from .models import DashboardElements, DashboardState, LivePowerData
from .snapshot import RecentData, load_recent_data
from .tables import mark_final_total_row

class RecentTab:
    def __init__(
        self,
        config: Config,
        state: DashboardState,
        elements: DashboardElements,
        live_collector: LivePowerCollector,
    ) -> None:
        self.config = config
        self.state = state
        self.elements = elements
        self.live_collector = live_collector
        self.timezone = ZoneInfo(config.timezone)
        self._today_values: dict[str, float | None] = {}
        self._previous_live_for_today: LivePowerData | None = None
        self._live_update_flash_id = 0

    def render_recent_tab(self, loaded_data: RecentData) -> None:
        try:
            day, full_updated_last_local, live_power, recent_daily, recent_monthly = loaded_data
        except Exception as error:
            LOGGER.exception("Dashboard Recent tab initial load failed")
            ui.label(f"Unable to load Recent data: {error}").classes("text-red-600")
            return


        self.elements.live_status_label = ui.label().classes("live-data-freshness text-sm mt-1")
        self._update_live_status(live_power.collected_at_utc)

        with ui.row().classes("items-center gap-1"):
            ui.label("Live / Today").classes("text-base font-semibold")
            self.elements.live_update_indicator = ui.spinner("dots", size="0.8rem").classes(
                "live-update-indicator text-slate-400"
            ).style("display: none").tooltip("Live table updated")
            self.elements.live_update_message = ui.label("Waiting for live update…").classes(
                "live-update-message text-xs text-slate-400"
            )

        self.elements.battery_status_label = ui.label(
            self._battery_status_text(live_power)
        ).classes("text-sm font-medium mt-1")

        latest = self._live_table_values(live_power)
        self._set_full_updated_timestamp(full_updated_last_local)
        self._set_live_today_values(live_power)

        self.elements.live_table = render_live_today_table(
            day,
            latest,
        )
        self._update_live_table_rows(live_power)

        self.elements.recent_daily_table = render_energy_summary_table(
            recent_daily, "Past 7 days (kWh)", "Date"
        )
        self.elements.recent_monthly_table = render_energy_summary_table(
            recent_monthly, "Past 12 months (kWh)", "Month"
        )
        self.elements.live_timestamp = live_power.collected_at_utc

    def refresh_recent_tab(self) -> None:
        self.apply_loaded_data(self._load_data())

    def apply_loaded_data(self, loaded_data) -> None:
        day, full_updated_last_local, live_power, recent_daily, recent_monthly = loaded_data
        latest = self._live_table_values(live_power)
        self._set_full_updated_timestamp(full_updated_last_local)
        self._set_live_today_values(live_power)
        if self.elements.live_table is not None:
            self.elements.live_table.rows = data.summary_rows(
                day,
                latest,
            )
            self._update_live_table_rows(live_power)
        if self.elements.battery_status_label is not None:
            self.elements.battery_status_label.set_text(self._battery_status_text(live_power))
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
            self._advance_today_values(live_power)
            self._update_live_table_rows(live_power)
            self.elements.live_timestamp = live_power.collected_at_utc
            self._flash_live_update_indicator()
            if self.elements.battery_status_label is not None:
                self.elements.battery_status_label.set_text(self._battery_status_text(live_power))
        self._update_live_status(live_power.collected_at_utc)
        self._set_full_updated_timestamp(self.elements.full_updated_last_local)

    def _set_full_updated_timestamp(self, full_updated_last_local: str | None) -> None:
        self.elements.full_updated_last_local = full_updated_last_local
        label = self.elements.updated_at_label
        if label is None:
            return

        stale_after_seconds = (
            self.config.actuals.data_retrival_schedule.full_updated_interval_seconds * 3
        )
        label.props(add=(
            f'data-last-full-update="{full_updated_last_local or ""}" '
            f'data-stale-after-seconds="{stale_after_seconds}"'
        ))

    def _load_data(self) -> RecentData:
        LOGGER.info("Dashboard loading data from database")
        loaded_data = load_recent_data(self.config, self.live_collector)
        return loaded_data

    @staticmethod
    def _live_table_values(live_power: LivePowerData) -> dict[str, float | None]:
        return live_power.values

    def _set_live_today_values(
        self,
        live_power: LivePowerData,
    ) -> None:
        self._today_values = {
            "Solar": None,
            "Battery": None,
            "Load": None,
            "Grid-Imported": None,
            "Grid-Exported": None,
        }
        self._apply_live_today_meters(live_power)
        self._previous_live_for_today = live_power

    def _update_live_table_rows(self, live_power: LivePowerData) -> None:
        table = self.elements.live_table
        if table is None:
            return
        rows: list[dict[str, Any]] = []
        for existing_row in table.rows:
            row = dict(existing_row)
            key = str(row.get("latest_key", ""))
            if key in live_power.values:
                row["latest"] = data.format_dashboard_number(live_power.values[key])
            metric = str(row.get("metric", ""))
            if metric in self._today_values:
                row["today"] = data.format_dashboard_number(self._today_values[metric])
            rows.append(row)
        table.rows = rows

    def _flash_live_update_indicator(self) -> None:
        """Briefly show the live-update indicator for a received telemetry sample."""
        indicator = self.elements.live_update_indicator
        if indicator is None:
            return
        self._live_update_flash_id += 1
        # The browser observes this attribute and owns the three-second
        # timeout.  No server timer can remain queued after a newer sample.
        indicator.props(add=f'data-live-update-sequence="{self._live_update_flash_id}"')

    def _apply_live_today_meters(self, live_power: LivePowerData) -> None:
        direct_values = {
            "Solar": live_power.values.get("today_solar"),
            "Load": live_power.values.get("today_load"),
            "Grid-Imported": live_power.values.get("today_grid_import"),
            "Grid-Exported": live_power.values.get("today_grid_export"),
        }
        charge = live_power.values.get("today_battery_charge")
        discharge = live_power.values.get("today_battery_discharge")
        if charge is not None and discharge is not None:
            direct_values["Battery"] = charge - discharge
        for metric, value in direct_values.items():
            if value is not None:
                self._today_values[metric] = value

    def _advance_today_values(self, live_power: LivePowerData) -> None:
        previous = self._previous_live_for_today
        self._previous_live_for_today = live_power
        self._apply_live_today_meters(live_power)
        if previous is None or previous.collected_at_utc is None or live_power.collected_at_utc is None:
            return
        previous_time = data.parse_time(previous.collected_at_utc, ZoneInfo("UTC"))
        current_time = data.parse_time(live_power.collected_at_utc, ZoneInfo("UTC"))
        if previous_time.astimezone(self.timezone).date() != current_time.astimezone(self.timezone).date():
            self._set_live_today_values(live_power)

    @staticmethod
    def _battery_status_text(live_power: LivePowerData) -> str:
        if live_power.collected_at_utc is None:
            return ""
        return data.battery_status_text(live_power.values)

    def _log_database_staleness(self, collected_at_utc: str | None) -> None:
        if collected_at_utc is None:
            LOGGER.warning("Dashboard database telemetry is unavailable")
            return
        age_seconds = (
            datetime.now(ZoneInfo("UTC"))
            - data.parse_time(collected_at_utc, ZoneInfo("UTC"))
        ).total_seconds()
        if age_seconds > self.config.actuals.data_retrival_schedule.full_updated_interval_seconds * 2:
            LOGGER.warning("Dashboard database telemetry is stale")

    def _update_live_status(self, collected_at_utc: str | None) -> None:
        label = self.elements.live_status_label
        if label is None:
            return
        label.classes(replace="text-sm mt-1")
        label.props(add=(
            f'data-last-live-update="{collected_at_utc or ""}" '
            'data-stale-after-seconds="900"'
        ))
        if collected_at_utc is None:
            label.set_text("")
            label.set_visibility(False)
            return
        age_seconds = max(
            0.0,
            (datetime.now(ZoneInfo("UTC")) - data.parse_time(collected_at_utc, ZoneInfo("UTC"))).total_seconds(),
        )
        if age_seconds > 60 * 15:
            LOGGER.warning("Dashboard live data is stale")
            label.set_text(f"Data is out of date — last updated {data.human_readable_age(age_seconds)}. Refreshing…")
            label.set_visibility(True)
            label.classes(add="text-orange-700 font-semibold")
            return
        label.set_text("")
        label.set_visibility(False)


def render_live_today_table(
    dataframe: pd.DataFrame,
    latest: dict[str, float | None],
) -> Table:
    columns = [
        {"name": "metric", "label": "", "field": "metric", "align": "left"},
        {"name": "latest", "label": "Latest (kW)", "field": "latest", "align": "right"},
        {"name": "today", "label": "Today (kWh)", "field": "today", "align": "right"},
        {"name": "latest_forecast", "label": "Latest Forecast (kWh)", "field": "latest_forecast", "align": "right"},
        {"name": "forecast", "label": "BoD Forecast (kWh)", "field": "forecast", "align": "right"},
        {"name": "forecast_raw", "label": "Raw Forecast (kWh)", "field": "forecast_raw", "align": "right"},
    ]
    return ui.table(
        columns=columns,
        rows=data.summary_rows(dataframe, latest),
        row_key="metric",
    ).props("dense flat bordered").classes("live-today-table max-w-full").style("width: fit-content")


def render_energy_summary_table(dataframe: pd.DataFrame, title: str, period_label: str) -> Table:
    ui.label(title).classes("text-base font-semibold mt-4")
    columns = [
        {"name": "period", "label": period_label, "field": "period", "align": "left"},
        {"name": "solar_actual", "label": "Solar Actual", "field": "solar_actual", "align": "right"},
        {"name": "solar_forecast", "label": "Solar Forecast", "field": "solar_forecast", "align": "right"},
        {"name": "load", "label": "Load", "field": "load", "align": "right"},
        {"name": "grid_import", "label": "Grid Import", "field": "grid_import", "align": "right"},
        {"name": "grid_export", "label": "Grid Export", "field": "grid_export", "align": "right"},
        {"name": "grid_import_cost", "label": "Import Cost", "field": "grid_import_cost", "align": "right"},
        {"name": "grid_export_revenue", "label": "Export Revenue", "field": "grid_export_revenue", "align": "right"},
        {"name": "net_cost", "label": "Net Cost", "field": "net_cost", "align": "right"},
        {"name": "no_solar_battery_import_cost", "label": "No Solar Cost", "field": "no_solar_battery_import_cost", "align": "right"},
    ]
    return mark_final_total_row(
        ui.table(columns=columns, rows=data.energy_summary_rows(dataframe), row_key="period")
        .props("dense flat bordered")
        .classes("max-w-full")
        .style("width: fit-content")
    )
