from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nicegui import run, ui

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
        self.desktop_controls = None
        self.mobile_controls_trigger = None
        self.mobile_controls_drawer = None

    def render_historical_controls(self):
        self.desktop_controls = self._render_controls(compact=False)
        return self.desktop_controls

    def render_mobile_historical_controls(self) -> None:
        """Render the small-screen control drawer at NiceGUI's layout root."""
        with ui.left_drawer(value=False, fixed=True, bordered=True).props("overlay") as drawer:
            drawer.classes("analysis-mobile-drawer")
            ui.label("Analysis controls").classes("text-lg font-semibold")
            self._render_controls(compact=True, after_action=drawer.hide)
        self.mobile_controls_drawer = drawer

    def render_mobile_controls_trigger(self):
        self.mobile_controls_trigger = ui.button(icon="menu", on_click=self.mobile_controls_drawer.toggle).props("round flat dense").classes("analysis-mobile-controls-trigger")
        return self.mobile_controls_trigger

    def set_controls_visibility(self, visible: bool) -> None:
        if self.desktop_controls is not None:
            self.desktop_controls.set_visibility(visible)
        if self.mobile_controls_trigger is not None:
            self.mobile_controls_trigger.classes(
                remove="analysis-mobile-controls-trigger-hidden"
                if visible
                else None,
                add=None if visible else "analysis-mobile-controls-trigger-hidden",
            )
        if not visible and self.mobile_controls_drawer is not None:
            self.mobile_controls_drawer.hide()

    def _render_controls(self, *, compact: bool, after_action=None):
        classes = (
            "analysis-controls analysis-controls-mobile w-full gap-3"
            if compact
            else "analysis-controls analysis-controls-desktop w-full items-end gap-3"
        )
        container = ui.column() if compact else ui.row()
        with container.classes(classes) as controls:
            if compact:
                with ui.row().classes("w-full items-end gap-2"):
                    ui.button("<", on_click=lambda: move_as_of(-1)).props("dense outline")
                    as_of_input = ui.input("As of", value=self.state.historical_as_of.isoformat()).props(f"type=date outlined dense max={datetime.now(self.timezone).date().isoformat()}").classes("flex-grow")
                    ui.button(">", on_click=lambda: move_as_of(1)).props("dense outline")
            else:
                ui.button("<", on_click=lambda: move_as_of(-1)).props("dense outline")
                as_of_input = ui.input("As of", value=self.state.historical_as_of.isoformat()).props(f"type=date outlined dense max={datetime.now(self.timezone).date().isoformat()}").classes("w-40").style("width: 8rem")
                ui.button(">", on_click=lambda: move_as_of(1)).props("dense outline")

            count_input = ui.number("Last", value=self.state.historical_count, min=1, step=1).props("outlined dense").classes("w-full" if compact else "w-28").style("" if compact else "width: 4.2rem")
            unit_input = ui.select(
                ["hours", "days", "weeks", "months", "years"],
                value=self.state.historical_unit,
                label="Period",
            ).props("outlined dense").classes("w-full" if compact else "w-36").style("" if compact else "width: 6.75rem")
            frequency_input = ui.select(
                ["hour", "day", "week", "month", "season", "year"],
                value=self.state.historical_frequency,
                label="Group by",
            ).props("outlined dense").classes("w-full" if compact else "w-48").style("" if compact else "width: 9rem")
            aggregation_input = ui.select(
                ["Sum", "Average"],
                value=self.state.historical_aggregation,
                label="Aggregation",
            ).props("outlined dense").classes("w-full" if compact else "w-32").style("" if compact else "width: 7.2rem")

            def selected_range() -> tuple[date, int, str, str, str] | None:
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
                aggregation = str(aggregation_input.value)
                if aggregation not in {"Sum", "Average"}:
                    ui.notify("Select a valid aggregation", type="negative")
                    return None
                return as_of, count, unit, frequency, aggregation

            with ui.row().classes("items-center gap-2") as progress:
                ui.spinner(size="sm")
                ui.label("Updating...").classes("text-sm")
            progress.set_visibility(False)

            apply_button = None
            loading = False

            async def apply_range() -> None:
                nonlocal loading
                if loading:
                    return
                selection = selected_range()
                if selection is None:
                    return
                as_of, count, unit, frequency, aggregation = selection
                self.state.historical_as_of = as_of
                self.state.historical_count = count
                self.state.historical_unit = unit
                self.state.historical_frequency = frequency
                self.state.historical_aggregation = aggregation
                bounds = data.analysis_range_bounds(as_of, count, unit, self.timezone)
                self.state.historical_power_interval_minutes = (
                    1 if bounds.start_date == bounds.end_date else 60
                )
                loading = True
                progress.set_visibility(True)
                if apply_button is not None:
                    apply_button.set_visibility(False)
                try:
                    analysis_data = await run.io_bound(
                        data.load_analysis_range,
                        self.config.database.path,
                        self.config.timezone,
                        bounds,
                        frequency,
                        self.state.historical_power_interval_minutes,
                        self.config.forecast.arrays,
                        self.config.dashboard.actuals_to_forecast,
                        aggregation.lower(),
                    )
                    self._update_historical_charts(analysis_data, refresh_battery=True)
                    if after_action is not None:
                        after_action()
                except Exception as error:
                    ui.notify(f"Unable to update historical data: {error}", type="negative")
                finally:
                    loading = False
                    progress.set_visibility(False)
                    if apply_button is not None:
                        apply_button.set_visibility(True)

            def download_data() -> None:
                selection = selected_range()
                if selection is None:
                    return
                as_of, count, unit, _, _ = selection
                bounds = data.analysis_range_bounds(as_of, count, unit, self.timezone)
                show_download_data_dialog(self.config.database.path, bounds, self.timezone)
                if after_action is not None:
                    after_action()

            async def move_as_of(direction: int) -> None:
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
                # Keep the same async Apply path for the date navigation buttons.
                await apply_range()

            apply_button = ui.button("Apply", on_click=apply_range, icon="date_range").classes("w-full" if compact else "")
            if not compact:
                ui.space()
            ui.button("Download Data", on_click=download_data, icon="download").classes("w-full" if compact else "")

        return controls

    def render_historical_tab(self) -> None:
        try:
            analysis_data = self._load_historical_data()
        except Exception as error:
            LOGGER.exception("Dashboard historical data initial load failed")
            ui.notify(f"Unable to load historical data: {error}", type="negative")
            return

        self.elements.historical_charts = charts.render_historical_charts(
            analysis_data.energy,
            analysis_data.power,
            analysis_data.battery,
            self.state,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
            self._refresh_historical_time_zoom,
        )

    def _update_historical_charts(self, analysis_data, *, refresh_battery: bool = True) -> None:
        historical = analysis_data.energy
        power = analysis_data.power
        battery = analysis_data.battery

        chart_elements = self.elements.historical_charts
        if chart_elements is None:
            return
        chart_elements.energy.update(historical)
        chart_elements.power.update(power)
        if refresh_battery:
            chart_elements.battery.update(battery)
        chart_elements.array_energy.update(historical)
        chart_elements.money.update(charts.money_dataframe(historical))

    def _load_historical_data(self):
        bounds = data.analysis_range_bounds(
            self.state.historical_as_of,
            self.state.historical_count,
            self.state.historical_unit,
            self.timezone,
        )
        return data.load_analysis_range(
            self.config.database.path,
            self.config.timezone,
            bounds,
            self.state.historical_frequency,
            self.state.historical_power_interval_minutes,
            self.config.forecast.arrays,
            self.config.dashboard.actuals_to_forecast,
        )

    def _refresh_historical_time_zoom(self, event) -> None:
        zoom_range = charts.data_zoom_range(event)
        if zoom_range is None or charts.zoom_ranges_match(
            zoom_range,
            (self.state.historical_time_zoom_start, self.state.historical_time_zoom_end),
        ):
            return
        self.state.historical_time_zoom_start, self.state.historical_time_zoom_end = zoom_range
        bounds = data.analysis_range_bounds(
            self.state.historical_as_of,
            self.state.historical_count,
            self.state.historical_unit,
            self.timezone,
        )
        if bounds.start_date == bounds.end_date:
            # A one-day chart already contains minute points; zooming is a
            # client-side operation and must not reload a coarser dataset.
            return
        interval = (
            60
        )
        if interval != self.state.historical_power_interval_minutes:
            self.state.historical_power_interval_minutes = interval
            return
        chart_elements = self.elements.historical_charts
        if chart_elements is not None:
            chart = chart_elements.power.chart
            if chart is not getattr(event, "sender", None):
                chart.run_chart_method("dispatchAction", {"type": "dataZoom", "start": zoom_range[0], "end": zoom_range[1]})
