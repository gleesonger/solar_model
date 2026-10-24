from __future__ import annotations

from solar_model.dashboard import formatting

from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui
from nicegui.elements.table import Table

from solar_model.logging_setup import LOGGER
from solar_model.config import Config

import solar_model.storage.queries.dashboard as data
from solar_model.dashboard.live import LivePowerCollector
from solar_model.dashboard.state import DashboardElements, DashboardState
from solar_model.storage.queries.types import LivePowerData, SUMMARY_LATEST_KEYS
from solar_model.dashboard.snapshot import RecentData, load_recent_data
from solar_model.dashboard.components.tables import mark_final_total_row
from solar_model.dashboard.components.warnings import DashboardWarnings, notify_error

class RecentTab:
    def __init__(
        self,
        config: Config,
        state: DashboardState,
        elements: DashboardElements,
        live_collector: LivePowerCollector,
        warnings: DashboardWarnings,
    ) -> None:
        self.config = config
        self.state = state
        self.elements = elements
        self.live_collector = live_collector
        self.warnings = warnings
        self.timezone = ZoneInfo(config.timezone)
        array_names = {array.panel_id: array.name for array in config.forecast.arrays}
        self._summary_latest_keys = {
            "Solar": "solar",
            **{
                f" - {array_names[panel_id]}": pv_string
                for pv_string, panel_id in config.dashboard.actuals_to_forecast.items()
            },
            **{label: key for label, key in SUMMARY_LATEST_KEYS.items() if label != "Solar"},
        }
        self._today_values: dict[str, float | None] = {}
        self._previous_live_for_today: LivePowerData | None = None

    def render_recent_tab(self, loaded_data: RecentData) -> None:
        try:
            day, full_updated_last_local, live_power, recent_daily, recent_monthly = loaded_data
        except Exception as error:
            LOGGER.exception("Dashboard Recent tab initial load failed")
            notify_error(f"Unable to load Recent data: {error}")
            return


        self.warnings.update_live(live_power.collected_at_utc)

        with ui.row().classes("items-center gap-1"):
            ui.label("Live / Today").classes("text-base font-semibold")
            self.elements.live_update_indicator = ui.element("span").props('aria-hidden="true"').classes(
                "live-update-indicator text-slate-500"
            ).tooltip("New live reading received")
            with self.elements.live_update_indicator:
                for _ in range(3):
                    ui.element("span").classes("live-update-dot")

        self.elements.battery_status_label = ui.label(
            self._battery_status_text(live_power)
        ).classes("text-sm font-medium mt-1")

        latest = self._live_table_values(live_power)
        self.warnings.update_full(full_updated_last_local)
        self._set_live_today_values(live_power)

        self.elements.live_table = render_live_today_table(
            day,
            latest,
            self._summary_latest_keys,
        )
        self._update_live_table_rows(live_power)

        self.elements.live_update_message = ui.label("Last reading: awaiting live data").classes(
            "live-update-message text-xs text-slate-400"
        )

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
        self.warnings.update_full(full_updated_last_local)
        self._set_live_today_values(live_power)
        if self.elements.live_table is not None:
            self.elements.live_table.rows = formatting.summary_rows(
                day,
                latest,
                self._summary_latest_keys,
            )
            self._update_live_table_rows(live_power)
        if self.elements.battery_status_label is not None:
            self.elements.battery_status_label.set_text(self._battery_status_text(live_power))
        if self.elements.recent_daily_table is not None:
            self.elements.recent_daily_table.rows = formatting.energy_summary_rows(recent_daily)
        if self.elements.recent_monthly_table is not None:
            self.elements.recent_monthly_table.rows = formatting.energy_summary_rows(recent_monthly)
        self.warnings.update_live(live_power.collected_at_utc)
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
            if self.elements.battery_status_label is not None:
                self.elements.battery_status_label.set_text(self._battery_status_text(live_power))
            self._show_live_update_indicator()
        self.warnings.update_live(live_power.collected_at_utc)

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
            label: None for label in self._summary_latest_keys
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
                row["latest"] = formatting.format_dashboard_number(live_power.values[key])
            metric = str(row.get("metric", ""))
            if metric in self._today_values:
                row["today"] = formatting.format_dashboard_number(self._today_values[metric])
            rows.append(row)
        table.rows = rows

    def _apply_live_today_meters(self, live_power: LivePowerData) -> None:
        direct_values = {
            "Solar": live_power.values.get("today_solar"),
            "Load": live_power.values.get("today_load"),
            "Import": live_power.values.get("today_grid_import"),
            "Export": live_power.values.get("today_grid_export"),
        }
        direct_values.update({
            label: live_power.values.get(f"today_{pv_string}")
            for label, pv_string in self._summary_latest_keys.items()
            if pv_string.startswith("pv")
        })
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
        return formatting.battery_status_text(live_power.values)

    def _show_live_update_indicator(self) -> None:
        """Tell this browser that a new live reading was applied."""
        indicator = self.elements.live_update_indicator
        if indicator is not None:
            indicator.client.run_javascript("window.flashLiveUpdateIndicator?.()")


def render_live_today_table(
    dataframe: pd.DataFrame,
    latest: dict[str, float | None],
    latest_keys: dict[str, str] = SUMMARY_LATEST_KEYS,
) -> Table:
    columns = [
        {"name": "metric", "label": "", "field": "metric", "align": "left"},
        {"name": "latest", "label": "Latest (kW)", "field": "latest", "align": "right"},
        {"name": "today", "label": "Today (kWh)", "field": "today", "align": "right"},
        {"name": "latest_forecast", "label": "Latest Forecast (kWh)", "field": "latest_forecast", "align": "right"},
        {"name": "forecast", "label": "BoD Forecast (kWh)", "field": "forecast", "align": "right"},
        {"name": "forecast_raw", "label": "Raw Forecast (kWh)", "field": "forecast_raw", "align": "right"},
    ]
    table = ui.table(
        columns=columns,
        rows=formatting.summary_rows(dataframe, latest, latest_keys),
        row_key="metric",
    ).props("dense flat bordered").classes(
        "live-today-table max-w-full"
    ).style("width: fit-content")
    table.add_slot("body", r"""
        <q-tr :props="props" :class="{ 'solar-orientation': props.row.is_solar_orientation }">
            <q-td v-for="col in props.cols" :key="col.name" :props="props" :col-name="col.name">
                {{ col.value }}
            </q-td>
        </q-tr>
    """)
    return table


def render_energy_summary_table(dataframe: pd.DataFrame, title: str, period_label: str) -> Table:
    ui.label(title).classes("text-base font-semibold mt-4")
    columns = [
        {"name": "period", "label": period_label, "field": "period", "align": "left"},
        {"name": "solar_actual", "label": "Solar Actual", "field": "solar_actual", "align": "right"},
        {"name": "solar_forecast", "label": "Solar Forecast", "field": "solar_forecast", "align": "right"},
        {"name": "load", "label": "Load", "field": "load", "align": "right"},
        {"name": "load_forecast", "label": "Load (Forecast)", "field": "load_forecast", "align": "right"},
        {"name": "grid_import", "label": "Grid Import", "field": "grid_import", "align": "right"},
        {"name": "grid_export", "label": "Grid Export", "field": "grid_export", "align": "right"},
        {"name": "grid_import_cost", "label": "Import Cost", "field": "grid_import_cost", "align": "right"},
        {"name": "grid_export_revenue", "label": "Export Revenue", "field": "grid_export_revenue", "align": "right"},
        {"name": "net_cost", "label": "Net Cost", "field": "net_cost", "align": "right"},
        {"name": "no_solar_battery_import_cost", "label": "No Solar Cost", "field": "no_solar_battery_import_cost", "align": "right"},
    ]
    return mark_final_total_row(
        ui.table(columns=columns, rows=formatting.energy_summary_rows(dataframe), row_key="period")
        .props("dense flat bordered")
        .classes("max-w-full")
        .style("width: fit-content")
    )
