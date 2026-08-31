from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import ui

from common import LOGGER
from config import Config

from . import charts, data
from .models import (
    DashboardElements,
    DashboardState,
    HOURLY_PERIODS,
)


class HourlyTab:
    def __init__(
        self,
        config: Config,
        state: DashboardState,
        elements: DashboardElements,
    ) -> None:
        self.config = config
        self.state = state
        self.elements = elements
        self.timezone = ZoneInfo(config.timezone)

    def render_hourly_tab(self) -> None:
        with ui.row().classes("w-full items-end gap-3"):
            start_input = ui.input("First date", value=self.state.hourly_start.isoformat()).props(
                "type=date outlined dense"
            )
            end_input = ui.input("End date", value=self.state.hourly_end.isoformat()).props(
                "type=date outlined dense"
            )
        updating_inputs = False

        def select_range(start: date, end: date, period: str) -> None:
            nonlocal updating_inputs
            self.state.hourly_start = start
            self.state.hourly_end = end
            self.state.hourly_period = period
            self.state.hourly_dates_valid = True
            self.state.power_interval_minutes = 60
            self.state.time_zoom_start = 0.0
            self.state.time_zoom_end = 100.0
            updating_inputs = True
            try:
                start_input.set_value(start.isoformat())
                end_input.set_value(end.isoformat())
            finally:
                updating_inputs = False
            period_button.set_text(period)
            self._try_refresh_hourly_tab()

        def date_range_changed() -> None:
            if updating_inputs:
                return
            try:
                start = date.fromisoformat(str(start_input.value))
                end = date.fromisoformat(str(end_input.value))
            except ValueError:
                self.state.hourly_dates_valid = False
                self._blank_hourly_tab()
                return
            if start > end:
                self.state.hourly_start = start
                self.state.hourly_end = end
                self.state.hourly_period = "Custom"
                self.state.hourly_dates_valid = False
                period_button.set_text("Custom")
                self._blank_hourly_tab()
                return
            select_range(start, end, data.hourly_period_name(start, end))

        def select_period_at_today(period: str, today: date) -> None:
            lifetime_start = (
                data.load_lifetime_start_date(self.config.database.path, today)
                if period == "Lifetime"
                else today
            )
            start, end = data.hourly_period_range(period, today, lifetime_start)
            select_range(start, end, period)

        def select_centre_period() -> None:
            today = datetime.now(self.timezone).date()
            if not self.state.hourly_dates_valid:
                select_period_at_today("Today", today)
            elif self.state.hourly_end != today:
                if self.state.hourly_period in HOURLY_PERIODS:
                    select_period_at_today(self.state.hourly_period, today)
                else:
                    span = (self.state.hourly_end - self.state.hourly_start).days + 1
                    select_range(today - timedelta(days=span - 1), today, "Custom")
            else:
                select_period_at_today(data.next_hourly_period(self.state.hourly_period), today)

        def move_period(direction: int) -> None:
            today = datetime.now(self.timezone).date()
            if not self.state.hourly_dates_valid:
                select_period_at_today("Today", today)
                return
            span = (self.state.hourly_end - self.state.hourly_start).days + 1
            start = self.state.hourly_start + timedelta(days=direction * span)
            end = self.state.hourly_end + timedelta(days=direction * span)
            if end > today:
                end = today
                start = today - timedelta(days=span - 1)
            select_range(start, end, self.state.hourly_period)

        with ui.row().classes("items-center gap-2 mt-2"):
            ui.button("<", on_click=lambda: move_period(-1)).props("dense outline")
            period_button = ui.button(
                self.state.hourly_period,
                on_click=select_centre_period,
            ).props("dense outline").classes("min-w-24")
            ui.button(">", on_click=lambda: move_period(1)).props("dense outline")
        start_input.on_value_change(date_range_changed)
        end_input.on_value_change(date_range_changed)

        try:
            hourly_range = self._load_hourly_range()
        except Exception as error:
            LOGGER.exception("Dashboard hourly data initial load failed")
            ui.label(f"Unable to load hourly data: {error}").classes("text-red-600")
            return
        self.elements.hourly_range_label = ui.label(self._range_label()).classes("text-sm text-gray-600 mb-2")
        self.elements.hourly_charts = charts.render_hourly_charts(
            hourly_range.energy,
            hourly_range.power,
            hourly_range.battery,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
            self.state.power_interval_minutes,
            self.state.time_zoom_start,
            self.state.time_zoom_end,
            self._refresh_time_zoom,
        )

    def refresh_hourly_tab(self) -> None:
        if not self.state.hourly_dates_valid or self.state.hourly_end < self.state.hourly_start:
            self._blank_hourly_tab()
            return
        self._display_hourly_range(self._load_hourly_range())

    def _try_refresh_hourly_tab(self) -> None:
        try:
            self.refresh_hourly_tab()
        except Exception as error:
            ui.notify(f"Unable to update hourly data: {error}", type="negative")

    def _load_hourly_range(self):
        return data.load_hourly_range(
            self.config.database.path,
            self.config.timezone,
            self.state.hourly_start,
            self.state.hourly_end,
            self.state.power_interval_minutes,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        )

    def _display_hourly_range(self, hourly_range: data.HourlyRangeData) -> None:
        if self.elements.hourly_range_label is not None:
            self.elements.hourly_range_label.set_text(self._range_label())
        chart_elements = self.elements.hourly_charts
        if chart_elements is None:
            return
        chart_elements.power_title.set_text(
            f"Average power by {charts.time_group_label(self.state.power_interval_minutes)} (kW)"
        )
        chart_elements.battery_title.set_text(
            "Average battery energy and state of charge by "
            f"{charts.time_group_label(self.state.power_interval_minutes)}"
        )
        charts.update_chart(chart_elements.energy, charts.energy_chart_options(hourly_range.energy, "hour"))
        charts.update_chart(
            chart_elements.power,
            charts.power_chart_options(
                hourly_range.power,
                self.config.forecast.arrays,
                self.config.dashboard.actuals_to_forecast,
                self.state.time_zoom_start,
                self.state.time_zoom_end,
            ),
        )
        charts.update_chart(chart_elements.battery, charts.battery_chart_options(hourly_range.battery))
        charts.update_chart(
            chart_elements.array_energy,
            charts.array_energy_chart_options(
                hourly_range.energy,
                "hour",
                self.config.forecast.arrays,
                self.config.dashboard.actuals_to_forecast,
            ),
        )

    def _blank_hourly_tab(self) -> None:
        self._display_hourly_range(data.empty_hourly_range(
            self.state.power_interval_minutes,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        ))
        if self.elements.hourly_range_label is not None:
            self.elements.hourly_range_label.set_text("Select a valid date range")

    def _refresh_time_zoom(self, event: Any) -> None:
        zoom_range = charts.data_zoom_range(event)
        if zoom_range is None or charts.zoom_ranges_match(
            zoom_range,
            (self.state.time_zoom_start, self.state.time_zoom_end),
        ):
            return
        self.state.time_zoom_start, self.state.time_zoom_end = zoom_range
        interval = charts.power_interval_for_zoom(*zoom_range)
        if interval != self.state.power_interval_minutes:
            self.state.power_interval_minutes = interval
            self._try_refresh_hourly_tab()
            return
        chart_elements = self.elements.hourly_charts
        if chart_elements is not None and chart_elements.power is not getattr(event, "sender", None):
            chart_elements.power.run_chart_method(
                "dispatchAction",
                {"type": "dataZoom", "start": zoom_range[0], "end": zoom_range[1]},
            )

    def _range_label(self) -> str:
        return (
            f"Hourly averages from {self.state.hourly_start:%d %b %Y} "
            f"to {self.state.hourly_end:%d %b %Y}, inclusive"
        )
