from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nicegui import ui

from common import LOGGER
from config import Config

from . import charts, data
from .download_data_dialog import show_download_data_dialog
from .models import DashboardElements, DashboardState


class AnalysisTab:
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

    def render_historical_controls(self):
        with ui.row().classes("analysis-controls w-full items-end gap-3") as controls:
            ui.button("<", on_click=lambda: move_as_of(-1)).props("dense outline")
            as_of_input = ui.input("As of", value=self.state.historical_as_of.isoformat()).props(
                f"type=date outlined dense max={datetime.now(self.timezone).date().isoformat()}"
            ).classes("w-40")
            ui.button(">", on_click=lambda: move_as_of(1)).props("dense outline")
            count_input = ui.number("Last", value=self.state.historical_count, min=1, step=1).props(
                "outlined dense"
            ).classes("w-28")
            unit_input = ui.select(
                ["hours", "days", "weeks", "months", "years"],
                value=self.state.historical_unit,
                label="Period",
            ).props("outlined dense").classes("w-36")
            frequency_input = ui.select(
                ["hour", "day", "week", "month", "season", "year"],
                value=self.state.historical_frequency,
                label="Group by",
            ).props("outlined dense").classes("w-48")

            def selected_range() -> tuple[date, int, str, str] | None:
                try:
                    raw_count = float(count_input.value)
                    count = int(raw_count)
                except (TypeError, ValueError):
                    ui.notify("Last must be a positive whole number", type="negative")
                    return None
                if count < 1 or raw_count != count:
                    ui.notify("Last must be a positive whole number", type="negative")
                    return None
                try:
                    as_of = date.fromisoformat(str(as_of_input.value))
                except ValueError:
                    ui.notify("As of must be a valid date", type="negative")
                    return None
                if as_of > datetime.now(self.timezone).date():
                    ui.notify("As of cannot be in the future", type="negative")
                    return None
                unit = str(unit_input.value)
                frequency = str(frequency_input.value)
                if unit not in {"hours", "days", "weeks", "months", "years"}:
                    ui.notify("Select a valid period", type="negative")
                    return None
                if frequency not in {"hour", "day", "week", "month", "season", "year"}:
                    ui.notify("Select a valid grouping", type="negative")
                    return None
                return as_of, count, unit, frequency

            def apply_range() -> None:
                selection = selected_range()
                if selection is None:
                    return
                as_of, count, unit, frequency = selection
                self.state.historical_as_of = as_of
                self.state.historical_count = count
                self.state.historical_unit = unit
                self.state.historical_frequency = frequency
                self._try_refresh_historical_tab()

            def download_data() -> None:
                selection = selected_range()
                if selection is None:
                    return
                as_of, count, unit, _ = selection
                bounds = data.analysis_range_bounds(as_of, count, unit, self.timezone)
                show_download_data_dialog(
                    self.config.database.path,
                    bounds,
                    self.timezone,
                )

            def move_as_of(direction: int) -> None:
                try:
                    as_of = date.fromisoformat(str(as_of_input.value))
                    raw_count = float(count_input.value)
                    count = int(raw_count)
                except (TypeError, ValueError):
                    ui.notify("Enter a valid As of date and Last value", type="negative")
                    return
                if count < 1 or raw_count != count:
                    ui.notify("Last must be a positive whole number", type="negative")
                    return
                unit = str(unit_input.value)
                if unit == "hours":
                    moved = as_of + timedelta(hours=direction * count)
                elif unit == "days":
                    moved = as_of + timedelta(days=direction * count)
                elif unit == "weeks":
                    moved = as_of + timedelta(weeks=direction * count)
                elif unit == "months":
                    moved = data.shift_date_by_months(as_of, direction * count)
                elif unit == "years":
                    moved = data.shift_date_by_months(as_of, direction * 12 * count)
                else:
                    ui.notify("Select a valid period", type="negative")
                    return
                as_of_input.set_value(min(moved, datetime.now(self.timezone).date()).isoformat())
                apply_range()

            ui.button("Apply", on_click=apply_range, icon="date_range")
            ui.space()
            ui.button("Download Data", on_click=download_data, icon="download")

        return controls

    def render_historical_tab(self) -> None:
        try:
            historical, power, battery, start, as_of = self._load_historical_data()
        except Exception as error:
            LOGGER.exception("Dashboard historical data initial load failed")
            ui.notify(f"Unable to load historical data: {error}", type="negative")
            return

        self.elements.historical_charts = charts.render_historical_charts(
            historical,
            power,
            battery,
            self.state,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
            self._refresh_historical_time_zoom,
        )

    def refresh_historical_tab(self) -> None:
        historical, power, battery, start, as_of = self._load_historical_data()

        chart_elements = self.elements.historical_charts
        if chart_elements is None:
            return
        chart_elements.energy.update(historical)
        chart_elements.power.update(power)
        chart_elements.battery.update(battery)
        chart_elements.array_energy.update(historical)
        chart_elements.money.update(charts.money_dataframe(historical))

    def _try_refresh_historical_tab(self) -> None:
        try:
            self.refresh_historical_tab()
        except Exception as error:
            ui.notify(f"Unable to update historical data: {error}", type="negative")

    def _load_historical_data(self):
        bounds = data.analysis_range_bounds(
            self.state.historical_as_of,
            self.state.historical_count,
            self.state.historical_unit,
            self.timezone,
        )
        analysis_data = data.load_analysis_range(
            self.config.database.path,
            self.config.timezone,
            bounds,
            self.state.historical_frequency,
            self.state.historical_power_interval_minutes,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        )
        return (
            analysis_data.energy,
            analysis_data.power,
            analysis_data.battery,
            bounds.start_date,
            bounds.end_date,
        )

    def _refresh_historical_time_zoom(self, event) -> None:
        zoom_range = charts.data_zoom_range(event)
        if zoom_range is None or charts.zoom_ranges_match(
            zoom_range,
            (self.state.historical_time_zoom_start, self.state.historical_time_zoom_end),
        ):
            return
        self.state.historical_time_zoom_start, self.state.historical_time_zoom_end = zoom_range
        interval = charts.power_interval_for_zoom(*zoom_range)
        if interval != self.state.historical_power_interval_minutes:
            self.state.historical_power_interval_minutes = interval
            self._try_refresh_historical_tab()
            return
        chart_elements = self.elements.historical_charts
        if chart_elements is not None:
            chart = chart_elements.power.chart
            if chart is not getattr(event, "sender", None):
                chart.run_chart_method(
                    "dispatchAction",
                    {"type": "dataZoom", "start": zoom_range[0], "end": zoom_range[1]},
                )
