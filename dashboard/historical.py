from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
            as_of_input = ui.input("As of", value=self.state.historical_as_of.isoformat()).props(
                f"type=date outlined dense max={datetime.now(self.timezone).date().isoformat()}"
            ).classes("w-40")
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
                try:
                    as_of = date.fromisoformat(str(as_of_input.value))
                except ValueError:
                    ui.notify("As of must be a valid date", type="negative")
                    return
                if as_of > datetime.now(self.timezone).date():
                    ui.notify("As of cannot be in the future", type="negative")
                    return
                unit = str(unit_input.value)
                frequency = str(frequency_input.value)
                if unit not in {"hours", "days", "weeks", "months", "years"}:
                    ui.notify("Select a valid period", type="negative")
                    return
                if frequency not in {"hour", "day", "week", "month", "season", "year"}:
                    ui.notify("Select a valid grouping", type="negative")
                    return
                self.state.historical_as_of = as_of
                self.state.historical_count = count
                self.state.historical_unit = unit
                self.state.historical_frequency = frequency
                self._try_refresh_historical_tab()

            ui.button("Apply", on_click=apply_range, icon="date_range")

        try:
            historical, power, battery, start, as_of = self._load_historical_data()
        except Exception as error:
            LOGGER.exception("Dashboard historical data initial load failed")
            ui.notify(f"Unable to load historical data: {error}", type="negative")
            return
        self.elements.historical_range_label = ui.label(
            self._range_label(start, as_of)
        ).classes("text-sm text-gray-600 mb-2")
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
        if self.elements.historical_range_label is not None:
            self.elements.historical_range_label.set_text(self._range_label(start, as_of))
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
        as_of = self.state.historical_as_of
        start_at, end_at = self._historical_time_bounds(as_of)
        start = (
            start_at.date()
            if start_at is not None
            else data.historical_start_date(
                as_of,
                self.state.historical_count,
                self.state.historical_unit,
            )
        )
        historical = data.load_historical_energy(
            self.config.database.path,
            self.config.timezone,
            start,
            as_of,
            self.state.historical_frequency,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
            start_at=start_at,
            end_at=end_at,
        )
        hourly_data = data.load_hourly_range(
            self.config.database.path,
            self.config.timezone,
            start,
            as_of,
            self.state.historical_power_interval_minutes,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        )
        return historical, hourly_data.power, hourly_data.battery, start, as_of

    def _historical_time_bounds(self, as_of: date) -> tuple[datetime | None, datetime | None]:
        if self.state.historical_unit not in {"hours", "minutes"}:
            return None, None
        today = datetime.now(self.timezone).date()
        end_at = (
            datetime.now(self.timezone)
            if as_of == today
            else datetime.combine(as_of, time.max, tzinfo=self.timezone)
        )
        if self.state.historical_unit == "hours":
            bucket_end = end_at.replace(minute=0, second=0, microsecond=0)
            return bucket_end - timedelta(hours=self.state.historical_count - 1), end_at
        bucket_end = end_at.replace(second=0, microsecond=0)
        return bucket_end - timedelta(minutes=self.state.historical_count - 1), end_at

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
            for display in (chart_elements.power, chart_elements.battery):
                chart = display.chart
                if chart is not getattr(event, "sender", None):
                    chart.run_chart_method(
                        "dispatchAction",
                        {"type": "dataZoom", "start": zoom_range[0], "end": zoom_range[1]},
                    )

    def _range_label(self, start, as_of) -> str:
        return (
            f"Total grouped by {self.state.historical_frequency}: "
            f"{start:%d %b %Y} to {as_of:%d %b %Y}, inclusive"
        )
