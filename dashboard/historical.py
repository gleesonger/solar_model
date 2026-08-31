from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from common import LOGGER
from config import Config

from . import charts, data
from .models import DashboardElements, DashboardState


class HistoricalTab:
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

    def render_historical_tab(self) -> None:
        with ui.row().classes("w-full items-end gap-3"):
            count_input = ui.number("Last", value=self.state.historical_count, min=1, step=1).props(
                "outlined dense"
            ).classes("w-28")
            unit_input = ui.select(
                ["days", "weeks", "months", "years"],
                value=self.state.historical_unit,
                label="Period",
            ).props("outlined dense").classes("w-36")
            frequency_input = ui.select(
                ["day", "week", "month", "season", "year"],
                value=self.state.historical_frequency,
                label="Group by",
            ).props("outlined dense").classes("w-36")

            def apply_range() -> None:
                try:
                    raw_count = float(count_input.value)
                    count = int(raw_count)
                except (TypeError, ValueError):
                    ui.notify("Last must be a positive whole number", type="negative")
                    return
                if count < 1 or raw_count != count:
                    ui.notify("Last must be a positive whole number", type="negative")
                    return
                unit = str(unit_input.value)
                frequency = str(frequency_input.value)
                if unit not in {"days", "weeks", "months", "years"}:
                    ui.notify("Select a valid period", type="negative")
                    return
                if frequency not in {"day", "week", "month", "season", "year"}:
                    ui.notify("Select a valid grouping", type="negative")
                    return
                self.state.historical_count = count
                self.state.historical_unit = unit
                self.state.historical_frequency = frequency
                self._try_refresh_historical_tab()

            ui.button("Apply", on_click=apply_range, icon="date_range")

        try:
            historical, start, today = self._load_historical_data()
        except Exception as error:
            LOGGER.exception("Dashboard historical data initial load failed")
            ui.label(f"Unable to load historical data: {error}").classes("text-red-600")
            return
        self.elements.historical_range_label = ui.label(
            self._range_label(start, today)
        ).classes("text-sm text-gray-600 mb-2")
        self.elements.historical_charts = charts.render_historical_charts(
            historical,
            self.state.historical_frequency,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        )

    def refresh_historical_tab(self) -> None:
        historical, start, today = self._load_historical_data()
        if self.elements.historical_range_label is not None:
            self.elements.historical_range_label.set_text(self._range_label(start, today))
        chart_elements = self.elements.historical_charts
        if chart_elements is None:
            return
        chart_elements.energy_title.set_text(f"Energy by {self.state.historical_frequency} (kWh)")
        chart_elements.array_energy_title.set_text(
            f"Solar energy by array and {self.state.historical_frequency} (kWh)"
        )
        charts.update_chart(chart_elements.energy, charts.energy_chart_options(historical, "period"))
        charts.update_chart(
            chart_elements.array_energy,
            charts.array_energy_chart_options(
                historical,
                "period",
                self.config.forecast.arrays,
                self.config.dashboard.actuals_to_forecast,
            ),
        )

    def _try_refresh_historical_tab(self) -> None:
        try:
            self.refresh_historical_tab()
        except Exception as error:
            ui.notify(f"Unable to update historical data: {error}", type="negative")

    def _load_historical_data(self):
        today = datetime.now(self.timezone).date()
        start = data.historical_start_date(today, self.state.historical_count, self.state.historical_unit)
        return (
            data.load_historical_energy(
                self.config.database.path,
                self.config.timezone,
                start,
                today,
                self.state.historical_frequency,
                self.config.forecast.arrays,
                self.config.dashboard.actuals_to_forecast,
            ),
            start,
            today,
        )

    def _range_label(self, start, today) -> str:
        return (
            f"Total energy grouped by {self.state.historical_frequency}: "
            f"{start:%d %b %Y} to {today:%d %b %Y}, inclusive"
        )
