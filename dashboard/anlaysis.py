from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nicegui import run, ui

from common import LOGGER
from config import Config

from .warnings import notify_error

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
            ).props("outlined dense").classes("w-full" if compact else "w-32").style("" if compact else "width: 6rem")
            frequency_input = ui.select(
                ["hour", "day", "week", "month", "season", "year"],
                value=self.state.historical_frequency,
                label="Group by",
            ).props("outlined dense").classes("w-full" if compact else "w-36").style("" if compact else "width: 7rem")
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
                    notify_error("Last must be a positive whole number")
                    return None
                if count < 1 or raw_count != count:
                    notify_error("Last must be a positive whole number")
                    return None
                try:
                    as_of = date.fromisoformat(str(as_of_input.value))
                except ValueError:
                    notify_error("As of must be a valid date")
                    return None
                if as_of > datetime.now(self.timezone).date():
                    notify_error("As of cannot be in the future")
                    return None
                unit = str(unit_input.value)
                frequency = str(frequency_input.value)
                if unit not in {"hours", "days", "weeks", "months", "years"}:
                    notify_error("Select a valid period")
                    return None
                if frequency not in {"hour", "day", "week", "month", "season", "year"}:
                    notify_error("Select a valid grouping")
                    return None
                aggregation = str(aggregation_input.value)
                if aggregation not in {"Sum", "Average"}:
                    notify_error("Select a valid aggregation")
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
                loading = True
                progress.set_visibility(True)
                if apply_button is not None:
                    apply_button.set_visibility(False)
                try:
                    bounds = data.analysis_range_bounds(as_of, count, unit, self.timezone)
                    self.state.historical_power_interval_minutes = (
                        1 if bounds.start_date == bounds.end_date else 60
                    )
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
                    notify_error(f"Unable to update historical data: {error}")
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
                    notify_error("Enter a valid As of date and Last value")
                    return
                if count < 1 or raw_count != count:
                    notify_error("Last must be a positive whole number")
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
                    notify_error("Select a valid period")
                    return
                as_of_input.set_value(min(moved, datetime.now(self.timezone).date()).isoformat())
                # Keep the same async Apply path for the date navigation buttons.
                await apply_range()

            apply_button = ui.button("Apply", on_click=apply_range, icon="date_range").classes("w-full" if compact else "")
            if not compact:
                ui.space()
            with ui.row().classes("w-full gap-2" if compact else "items-center gap-2"):
                ui.button("Custom chart", on_click=self._show_custom_chart_builder, icon="add_chart").classes(
                    "flex-grow" if compact else ""
                )
                ui.button("Download Data", on_click=download_data, icon="download").classes(
                    "flex-grow" if compact else ""
                )

        return controls

    def _show_custom_chart_builder(self) -> None:
        """Open a live custom-chart preview without changing the main Analysis view."""
        metric_labels = charts.analysis_column_labels(self.config.forecast.arrays)
        orientation_metrics = {
            data.actual_column_name(array.panel_id): f"Solar - {array.name}"
            for array in self.config.forecast.arrays
            if array.panel_id in self.config.dashboard.actuals_to_forecast.values()
        }
        metric_labels.update(orientation_metrics)
        metric_labels["battery"] = "Battery Level"
        metric_options = {
            "solar": metric_labels["solar"],
            **orientation_metrics,
            **{
                metric: metric_labels[metric]
                for metric in data.CUSTOM_CHART_ENERGY_METRICS
                if metric != "solar" and metric in metric_labels
            },
        }
        selections_input = None
        as_of_input = None
        count_input = None
        period_input = None
        group_by_input = None
        split_by_input = None
        aggregation_input = None
        preview_chart = None
        legend_container = None
        preview_spinner = None
        refresh_button = None
        loading = False
        preview_data: dict | None = None
        visible_metrics: set[str] = set()
        visible_splits: set[str] = set()

        with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 90rem"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Custom chart builder").classes("text-lg font-semibold")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")
            with ui.row().classes("w-full gap-3"):
                as_of_input = ui.input(
                    "As of", value=self.state.historical_as_of.isoformat(),
                ).props(f"type=date outlined max={datetime.now(self.timezone).date().isoformat()}").classes("w-32")
                count_input = ui.number(
                    "Last", value=self.state.historical_count, min=1, step=1,
                ).props("outlined").classes("w-20")
                period_input = ui.select(
                    ["hours", "days", "weeks", "months", "years"],
                    value=self.state.historical_unit, label="Period",
                ).props("outlined").classes("w-28")
                group_by_input = ui.select(
                    ["hour", "day", "week", "month", "season", "year"],
                    value=self.state.historical_frequency,
                    label="Group by",
                ).props("outlined").classes("w-28")
                aggregation_input = ui.select(
                    ["Sum", "Average"],
                    value=self.state.historical_aggregation,
                    label="Aggregation",
                ).props("outlined").classes("w-28")
                split_by_input = ui.select(
                    {None: "No split", "hour": "Hour", "day": "Day", "week": "Week", "month": "Month", "season": "Season", "year": "Year"},
                    value=None, label="Split by",
                ).props("outlined").classes("w-28")
                selections_input = ui.select(
                    metric_options,
                    label="Variables",
                    multiple=True,
                    value=[],
                ).props("outlined use-chips").classes("flex-grow")
                with ui.element("div").classes("relative w-32").style("height: 2.5rem"):
                    refresh_button = ui.button("Refresh", icon="refresh").classes("absolute inset-0 w-full")
                    with ui.row().classes("absolute inset-0 items-center justify-center gap-2") as preview_spinner:
                        ui.spinner(size="sm")
                        ui.label("Loading").classes("text-sm text-gray-600")
                    preview_spinner.set_visibility(False)
            with ui.row().classes("w-full items-stretch gap-3"):
                with ui.element("div").classes("flex-grow min-w-0"):
                    preview_chart = ui.echart(
                        charts.custom_chart_preview_options({"periods": [], "series": []}, metric_labels)
                    ).classes("w-full").style("height: 34rem")
                legend_container = ui.column().classes(
                    "w-52 shrink-0 overflow-auto rounded border p-2"
                ).style("height: 34rem")

            def update_chart() -> None:
                if preview_data is None:
                    return
                options = charts.custom_chart_preview_options(
                    preview_data,
                    metric_labels,
                    visible_metrics=visible_metrics,
                    visible_splits=(
                        visible_splits
                        if any(item.get("split") is not None for item in preview_data.get("series", []))
                        else None
                    ),
                )
                preview_chart.options.clear()
                preview_chart.options.update(options)
                preview_chart.update()

            def render_dimension_legend() -> None:
                legend_container.clear()
                if preview_data is None or not preview_data.get("series"):
                    return
                series = preview_data["series"]
                dimension_styles = charts.custom_chart_dimension_styles(series)
                metrics = dimension_styles["metrics"]
                splits = dimension_styles["splits"]
                with legend_container:
                    ui.label("Variables").classes("text-sm font-medium")
                    for metric in metrics:
                        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                            ui.html(charts.custom_chart_legend_sample(metrics[metric])).classes("shrink-0").tooltip(
                                metrics[metric].get("name", "Colour")
                            )
                            checkbox = ui.checkbox(
                                metric_labels.get(metric, metric.replace("_", " ").title()),
                                value=metric in visible_metrics,
                            ).props("dense").classes("min-w-0")

                        def metric_changed(event, *, selected_metric=metric) -> None:
                            if event.value:
                                visible_metrics.add(selected_metric)
                            else:
                                visible_metrics.discard(selected_metric)
                            update_chart()

                        checkbox.on_value_change(metric_changed)
                    if splits:
                        ui.separator().classes("my-2")
                        ui.label("Split by").classes("text-sm font-medium")
                        for split in splits:
                            with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                                ui.html(charts.custom_chart_legend_sample(splits[split])).classes("shrink-0").tooltip(
                                    splits[split].get("name", "Colour")
                                )
                                checkbox = ui.checkbox(split, value=split in visible_splits).props("dense").classes("min-w-0")

                            def split_changed(event, *, selected_split=split) -> None:
                                if event.value:
                                    visible_splits.add(selected_split)
                                else:
                                    visible_splits.discard(selected_split)
                                update_chart()

                            checkbox.on_value_change(split_changed)

            async def refresh_preview() -> None:
                nonlocal loading, preview_data
                if loading:
                    return
                selected = tuple(str(metric) for metric in (selections_input.value or []))
                split_by = split_by_input.value
                if split_by is not None:
                    split_by = str(split_by)
                selections = tuple((metric, split_by) for metric in selected)
                loading = True
                preview_spinner.set_visibility(True)
                refresh_button.set_visibility(False)
                refresh_button.disable()
                if not selections:
                    preview_data = {"periods": [], "series": []}
                try:
                    if selections:
                        try:
                            as_of = date.fromisoformat(str(as_of_input.value))
                            raw_count = float(count_input.value)
                            count = int(raw_count)
                        except (TypeError, ValueError):
                            notify_error("Enter a valid As of date and Last value")
                            return
                        if count < 1 or raw_count != count or as_of > datetime.now(self.timezone).date():
                            notify_error("As of must not be in the future and Last must be positive")
                            return
                        bounds = data.analysis_range_bounds(as_of, count, str(period_input.value), self.timezone)
                        preview = await run.io_bound(
                            data.load_custom_chart_preview,
                            self.config.database.path,
                            self.config.timezone,
                            bounds,
                            str(group_by_input.value),
                            str(aggregation_input.value).lower(),
                            self.config.forecast.arrays,
                            self.config.dashboard.actuals_to_forecast,
                            selections,
                        )
                        preview_data = preview
                    visible_metrics.clear()
                    visible_metrics.update(str(item["metric"]) for item in preview_data.get("series", []))
                    visible_splits.clear()
                    visible_splits.update(
                        str(item["split"])
                        for item in preview_data.get("series", [])
                        if item.get("split") is not None
                    )
                    render_dimension_legend()
                    update_chart()
                except Exception as error:
                    notify_error(f"Unable to build custom chart: {error}")
                finally:
                    loading = False
                    preview_spinner.set_visibility(False)
                    refresh_button.enable()
                    refresh_button.set_visibility(True)

            refresh_button.on_click(refresh_preview)
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Close", on_click=dialog.close).props("outline")
        dialog.open()

    def render_historical_tab(self) -> None:
        try:
            analysis_data = self._load_historical_data()
        except Exception as error:
            LOGGER.exception("Dashboard historical data initial load failed")
            notify_error(f"Unable to load historical data: {error}")
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
            self.state.historical_aggregation.lower(),
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
