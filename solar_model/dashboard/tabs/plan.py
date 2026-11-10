"""Per-browser energy-plan controls and presentation."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo
from typing import Any, Literal

from nicegui import run, ui
from nicegui.events import ValueChangeEventArguments
from nicegui.run import SubprocessException
import pandas as pd

from solar_model.config import Config, EnergyPlanConfig, energy_plan_yaml, parse_energy_plan_yaml, save_energy_plan_yaml
from solar_model.dashboard.components.warnings import notify_error, notify_success
from solar_model.dashboard.planning_worker import demonstrate
from solar_model.dashboard.plan_store import PlanRun, PlanStore
from solar_model.dashboard.components.plan_detail import detail_excel, render_detail_table
from solar_model.logging_setup import LOGGER
from solar_model.planning.types import NextAction, NoFeasiblePlanError, OperatingMode, SimulationResult
from solar_model.services.planning import PlanReport, generate_plan_report
from solar_model.services.plan_history import read_saved_run, read_report_actuals
from solar_model.storage.plan_history import list_plans


from solar_model.planning.plan_report import (
    Aggregation,
    PlanColumn,
    detail_column,
    detail_columns,
    scenario_average_cost,
    scenario_cost_summary,
    expected_plan_summary,
    scenario_summary,
    table_detail_frame,
    detail_frame,
    detail_rows,
    advanced_plan_frame,
    next_action_description,
    detail_totals,
    advanced_detail_totals,
    hourly_detail_frame,
    detail_decimal_places,
    PLAN_COLUMNS,
    ADVANCED_HIDDEN_COLUMNS,
    ADVANCED_LABELS,
)


def chart_hour(stamp: pd.Timestamp, timezone_name: str) -> pd.Timestamp:
    local = stamp.to_pydatetime().astimezone(ZoneInfo(timezone_name))
    return pd.Timestamp(local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc))


def hourly_forecast_bars(frame: pd.DataFrame, energy_column: str, timezone_name: str) -> list[list[float | None]]:
    totals: dict[pd.Timestamp, list[float]] = {}
    for start, end, energy in frame[["starts_at", "ends_at", energy_column]].itertuples(index=False, name=None):
        duration = (end - start).total_seconds()
        cursor = start
        while cursor < end:
            bucket = chart_hour(cursor, timezone_name)
            boundary = min(bucket + timedelta(hours=1), end)
            seconds = (boundary - cursor).total_seconds()
            total = totals.setdefault(bucket, [0., 0.])
            if pd.notna(energy):
                total[0] += float(energy) * seconds / duration
                total[1] += seconds
            cursor = boundary
    return [[(bucket + timedelta(minutes=30)).timestamp() * 1000, energy * 3600 / seconds if seconds else None] for bucket, (energy, seconds) in sorted(totals.items())]


def hourly_actual_bars(actuals: pd.DataFrame, column: str, timezone_name: str) -> list[list[float | None]]:
    if actuals.empty:
        return []
    totals: dict[pd.Timestamp, list[float]] = {}
    for stamp, value in actuals[["timestamp", column]].itertuples(index=False, name=None):
        total = totals.setdefault(chart_hour(stamp, timezone_name), [0., 0.])
        if pd.notna(value):
            total[0] += float(value)
            total[1] += 1
    return [[(bucket + timedelta(minutes=30)).timestamp() * 1000, total / count if count else None] for bucket, (total, count) in sorted(totals.items())]


def plan_chart_options(frame: pd.DataFrame, actuals: pd.DataFrame, timezone_name: str, forecast_type: Literal["line", "bar"] = "line") -> dict[str, Any]:
    """One legend entry per metric, with solid actuals and dashed scenario forecasts."""
    series = []
    if not actuals.empty:
        actuals = actuals[actuals.timestamp <= frame.ends_at.max()]
    hours = (pd.to_datetime(frame.ends_at) - pd.to_datetime(frame.starts_at)).dt.total_seconds() / 3600
    starts = [pd.Timestamp(value).timestamp() * 1000 for value in frame.starts_at]
    ends = [pd.Timestamp(value).timestamp() * 1000 for value in frame.ends_at]
    for label, column, color, axis in (
        ("Solar", "solar", "#F9C74F", 0),
        ("House Load", "house_load", "#0072B2", 0),
        ("Battery", "battery", "#56B4E9", 1),
        ("Grid Import", "grid_import", "#D1495B", 0),
        ("Grid Export", "grid_export", "#009E73", 0),
    ):
        observed = [[pd.Timestamp(row.timestamp).timestamp() * 1000,
                     None if pd.isna(getattr(row, column)) else float(getattr(row, column))]
                    for row in actuals.itertuples()]
        if column == "battery":
            opening = frame.battery_start_kwh.iloc[0]
            forecast = [[starts[0], float(opening) if pd.notna(opening) else None]] + list(zip(ends, frame.battery_end_kwh.astype(float)))
        else:
            energy_column = {"solar": "solar_kwh", "house_load": "load_kwh"}.get(column, column + "_kwh")
            values = (frame[energy_column].astype(float) / hours).tolist()
            forecast = list(zip(starts, values)) + [[ends[-1], values[-1]]]
        for kind, data in (("actual", observed), ("forecast", forecast)):
            is_bar = column != "battery" and forecast_type == "bar"
            if is_bar:
                data = hourly_forecast_bars(frame, energy_column, timezone_name) if kind == "forecast" else hourly_actual_bars(actuals[actuals.timestamp < frame.ends_at.max()] if not actuals.empty else actuals, column, timezone_name)
            stepped = kind == "forecast" and column in {"grid_import", "grid_export"}
            series.append({
                "id": f"{column}_{kind}",
                "name": label, "type": "bar" if is_bar else "line", "data": data, "yAxisIndex": axis,
                "z": 1 if is_bar else 2,
                "barMaxWidth": 12,
                "itemStyle": {"opacity": 0.65 if is_bar and kind == "forecast" else 1, "decal": {"symbol": "rect", "dashArrayX": [1, 0], "dashArrayY": [2, 4], "rotation": -0.785} if is_bar and kind == "forecast" else None},
                "showSymbol": False, "connectNulls": kind == "actual", "color": color,
                "step": "end" if stepped else False,
                "smooth": 0.2 if kind == "forecast" and not stepped else False,
                "smoothMonotone": "x",
                "lineStyle": {"type": "solid" if kind == "actual" else "dashed", "width": 2},
            })
    date_format = 'new Intl.DateTimeFormat("en-IE", {timeZone: ' + json.dumps(timezone_name) + ', day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"})'
    return {
        "animation": False,
        "useUTC": True,
        "legend": {"data": ["Solar", "House Load", "Battery", "Grid Import", "Grid Export"], "type": "scroll", "left": 10, "width": "65%", "top": 5},
        "grid": {"left": 55, "right": 60, "top": 85, "bottom": 70},
        "xAxis": {"type": "time", "splitNumber": 4, "axisLabel": {"hideOverlap": True, ":formatter": f"value => {date_format}.format(value).replace(', ', '\\n')"}},
        "yAxis": [{"type": "value", "name": "Power (kW)"}, {"type": "value", "name": "Battery (kWh)", "alignTicks": True}],
        "tooltip": {"trigger": "axis", ":formatter": "params => { if (!params.length) return ''; return " + date_format + ".format(params[0].value[0]) + params.map(p => '<br/>' + p.marker + p.seriesName + (p.seriesIndex % 2 ? ' (forecast): ' : ' (actual): ') + (p.value[1] == null ? '—' : Number(p.value[1]).toFixed(2))).join(''); }"},
        "dataZoom": [{"type": "inside"}, {"type": "slider", "bottom": 10}],
        "series": series,
    }


class PlanTab:
    def __init__(self, config: Config, config_path: str = "config.yaml", plan_store: PlanStore | None = None) -> None:
        self.config = config
        self.config_path = config_path
        self.plan_store = plan_store
        self.shared_version = 0
        self.demonstration = False
        self.run_metadata: PlanRun | None = None
        self.energy_plan_override: EnergyPlanConfig | None = None
        self.settings_text: str | None = None
        self.override_text: str | None = None
        self.report = None
        self.timezone = ZoneInfo(config.timezone)
        self.result: SimulationResult | None = None
        self.actuals = pd.DataFrame()
        self.chart = None
        self.running = False
        self.battery_capacity_kwh: float | None = None
        self.detail_view = "Compact"
        self.forecast_chart_type: Literal["line", "bar"] = "line"
        self.applied_adjustment: float | None = None
        self.applied_model: str | None = None
        self.historical_id: str | None = None
        self._history_request = 0
        self._date_request = 0
        self._actuals_running = False

    def render_plan_tab(self) -> None:
        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            self.settings_button = ui.button("Plan settings", icon="settings", on_click=self.open_plan_settings)
            self.history_date = ui.input("Plan date", value="", on_change=self._load_snapshot_options).props("type=date clearable outlined dense").classes("w-40")
            self.history_snapshot = ui.select({}, value=None, label="Snapshot", on_change=self._select_snapshot).props("clearable outlined dense").classes("w-64 max-w-full")
            with ui.row().classes("items-center gap-2") as self.loading:
                ui.spinner(size="sm")
                ui.label("Calculating plan…").classes("text-sm text-gray-600")
            self.loading.set_visibility(False)
        self.status = ui.label("Waiting for a new full SigEnergy reading to calculate the plan." if self.plan_store else "Run a demonstration using the latest available data.").classes("text-sm text-gray-600")
        self.output = ui.column().classes("w-full min-w-0 gap-4")
        self.apply_shared_plan()
        if self.plan_store is not None:
            ui.timer(60, self.refresh_actuals, immediate=False)

    async def refresh_actuals(self) -> None:
        if self.report is None or self._actuals_running:
            return
        report = self.report
        request = self._history_request
        self._actuals_running = True
        try:
            actuals = await run.io_bound(read_report_actuals, self.live_config, report)
            if self.report is not report or request != self._history_request:
                return
            self.actuals = actuals
            series = plan_chart_options(self._scenario_frame(), actuals, self.config.timezone, self.forecast_chart_type)["series"]
            self.chart.options["series"] = series
            self.chart.run_chart_method("setOption", {"series": series}, {"replaceMerge": ["series"]})
        except Exception:
            LOGGER.exception("Unable to refresh plan chart actuals")
        finally:
            self._actuals_running = False

    async def _load_snapshot_options(self) -> None:
        self._date_request += 1
        request = self._date_request
        self._history_request += 1
        self.historical_id = None
        self.apply_shared_plan(force=True)
        raw = self.history_date.value
        if not raw:
            self.history_snapshot.set_options({}, value=None)
            return
        try:
            day = date.fromisoformat(raw)
            start = datetime.combine(day, time.min, self.timezone)
            end = datetime.combine(day + timedelta(days=1), time.min, self.timezone)
            plans = await run.io_bound(list_plans, self.config.database.path, start, end)
            if request != self._date_request:
                return
            options = {plan.snapshot_id: plan.saved_at.astimezone(self.timezone).strftime("%H:%M:%S %Z") for plan in plans}
            self.history_snapshot.set_options(options, value=None)
            if not plans:
                self.status.set_text("No saved plans for this date. The latest plan remains displayed.")
                self.status.set_visibility(True)
        except Exception as error:
            if request == self._date_request:
                notify_error(f"Unable to load saved plans: {error}")

    async def _select_snapshot(self) -> None:
        snapshot_id = self.history_snapshot.value
        self._history_request += 1
        request = self._history_request
        if not snapshot_id:
            self.historical_id = None
            self.apply_shared_plan(force=True)
            return
        self.historical_id = snapshot_id
        self.loading.set_visibility(True)
        try:
            saved = await run.io_bound(read_saved_run, self.config, snapshot_id, True)
            if request != self._history_request:
                return
            if saved is None:
                raise ValueError("This snapshot is no longer retained")
            completed = PlanRun(saved.report, saved.settings, saved.began_at, saved.full_data_at, saved.completed_at, snapshot_id=saved.snapshot_id)
            self._apply_run(completed)
        except Exception as error:
            if request == self._history_request:
                self.status.set_text(f"Unable to load saved plan: {error}")
                self.status.set_visibility(True)
        finally:
            if request == self._history_request:
                self.loading.set_visibility(False)

    @property
    def live_config(self) -> Config:
        return self.plan_store.config if self.plan_store else self.config

    def _apply_run(self, completed: PlanRun) -> None:
        self.run_metadata = completed
        self._show_report(completed.report, completed.settings, completed.demonstration)

    def _show_report(self, report: PlanReport, settings: EnergyPlanConfig | str, demonstration: bool) -> None:
        self.report = report
        self.result, self.actuals = report.simulation, report.actuals
        self.battery_capacity_kwh = report.battery_capacity_kwh
        self.settings_text = settings if isinstance(settings, str) else (self.override_text if demonstration and self.override_text is not None else energy_plan_yaml(settings))
        model = report.model or (settings.optimiser.model if isinstance(settings, EnergyPlanConfig) else "unknown")
        self.applied_model = {"mix_integer_linear_prog": "MILP", "dynamic_programming": "DP"}.get(model, model)
        self.applied_adjustment = report.protection_preference if report.protection_preference is not None else (settings.protection_preference if isinstance(settings, EnergyPlanConfig) else None)
        self.demonstration = demonstration
        if not report.full_resolution:
            self.detail_view = "Compact"
        self.status.set_visibility(False)
        with self.output:
            self._render_result()

    def apply_shared_plan(self, force: bool = False) -> None:
        store = self.plan_store
        if store is None or not hasattr(self, "output"):
            return
        if self.historical_id is not None:
            return
        if store.current is not None and (force or store.version > self.shared_version):
            self.shared_version = store.version
            self._apply_run(store.current)
        self.loading.set_visibility(store.running or self.running)
        if store.last_error:
            previous = " The previous plan is still shown." if self.result is not None else ""
            self.status.set_text("Plan refresh failed: " + store.last_error + previous)
            self.status.set_visibility(True)
        elif self.result is not None:
            self.status.set_visibility(False)

    def open_plan_settings(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full").style("width: 938px; max-width: calc(100vw - 48px)"):
            ui.label("Energy plan settings").classes("text-lg font-semibold")
            ui.label("Run Demonstration creates a demonstration for this browser. Save To Disk applies settings to the background plan after the next full SigEnergy update.").classes("text-sm text-gray-600")
            editor = ui.textarea("YAML", value=self.settings_text if self.settings_text is not None else energy_plan_yaml(self.energy_plan_override or self.live_config.energy_plan)).props("outlined rows=20 input-class=font-mono").classes("w-full").style("max-height: 60vh; overflow: auto")
            error_label = ui.label().classes("text-negative whitespace-pre-wrap")

            async def apply() -> None:
                try:
                    self.apply_plan_settings(editor.value)
                except (ValueError, OSError) as error:
                    error_label.set_text(str(error))
                    return
                dialog.close()
                await self.run_demonstration()

            async def save() -> None:
                text = editor.value
                try:
                    parse_energy_plan_yaml(self.live_config, text)
                except ValueError as error:
                    error_label.set_text(str(error))
                    return
                with ui.dialog() as confirmation, ui.card():
                    ui.label("Save energy plan settings to disk?").classes("text-lg font-semibold")
                    ui.label("This replaces energy_plan in the configuration file and updates the running background plan settings.")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=lambda: confirmation.submit(False)).props("flat")
                        ui.button("Confirm save", on_click=lambda: confirmation.submit(True))
                confirmed = await confirmation
                confirmation.delete()
                if not confirmed:
                    return
                try:
                    self.save_plan_settings(text)
                except (ValueError, OSError) as error:
                    error_label.set_text(str(error))
                    return
                error_label.set_text("")
                notify_success("Energy plan settings saved to disk.")

            def load_global() -> None:
                editor.set_value(energy_plan_yaml(self.live_config.energy_plan))
                error_label.set_text("")

            with ui.row().classes("w-full items-center flex-wrap gap-2"):
                ui.button("Save To Disk", on_click=save).props("outline color=primary")
                ui.space()
                ui.button("Load Global", on_click=load_global).props("flat")
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Run Demonstration", on_click=apply)
        dialog.on("hide", dialog.delete)
        dialog.open()

    def save_plan_settings(self, text: str) -> None:
        if self.plan_store:
            self.plan_store.save_settings(text)
            self.energy_plan_override = None
        else:
            save_energy_plan_yaml(self.config_path, self.config, text)
        self.override_text = None

    def apply_plan_settings(self, text: str) -> None:
        self.energy_plan_override = parse_energy_plan_yaml(self.live_config, text)
        self.override_text = text

    async def run_demonstration(self) -> None:
        if self.running:
            return
        override = self.energy_plan_override
        settings = override or self.live_config.energy_plan
        shared_version = self.plan_store.version if self.plan_store else 0
        self.running = True
        self.historical_id = None
        self._history_request += 1
        if hasattr(self, "history_date"):
            self.history_date.set_value("")
            self.history_snapshot.set_value(None)
        self.loading.set_visibility(True)
        self.settings_button.disable()
        self.status.set_visibility(False)
        as_of = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            if self.plan_store:
                completed = await self.plan_store.demonstrate(settings)
                if shared_version == self.plan_store.version and self.historical_id is None:
                    self._apply_run(completed)
            else:
                demonstrated = await demonstrate(replace(self.config, energy_plan=settings))
                report = demonstrated["report"]
                if report is None:
                    raise RuntimeError("The plan calculation did not return a result")
                if self.historical_id is None:
                    self._show_report(report, settings, True)
        except Exception as error:
            if self.historical_id is not None or (self.plan_store and shared_version != self.plan_store.version):
                return
            if isinstance(error, SubprocessException) and error.original_type == NoFeasiblePlanError.__name__:
                error = NoFeasiblePlanError(error.original_message)
            if isinstance(error, NoFeasiblePlanError):
                LOGGER.warning("Unable to calculate plan: %s", error)
            else:
                LOGGER.exception("Dashboard plan refresh failed")
            previous = " The previous plan is still shown." if self.result is not None else ""
            self.status.set_text("Plan refresh failed." + previous)
            self.status.set_visibility(True)
            notify_error(f"Unable to calculate plan: {error}")
        finally:
            self.running = False
            self.loading.set_visibility(self.plan_store.running if self.plan_store else False)
            self.settings_button.enable()

    def _render_result(self) -> None:
        result = self.result
        first = result.results.get("med", next(iter(result.results.values())))
        action = result.next_action
        minutes = self.report.action_minutes or (15. if not self.report.full_resolution else (first.ends_at.iloc[0] - first.starts_at.iloc[0]).total_seconds() / 60)
        capacity = self.report.command_battery_capacity_kwh if self.report is not None else None
        first_name = "med" if "med" in result.results else next(iter(result.results))
        solar_export = (self.report.action_solar_export or {}).get(first_name, first.solar_to_grid_kwh.iloc[0] if "solar_to_grid_kwh" in first else 0.)
        description = next_action_description(action, solar_export, capacity)
        local_start = first.starts_at.iloc[0].astimezone(self.timezone)

        today = datetime.now(self.timezone).date()
        start_date = local_start.date()

        if start_date == today:
            prefix = "Today,"
        elif start_date == today - timedelta(days=1):
            prefix = "Yesterday,"
        else:
            prefix = local_start.strftime("%d/%m/%Y")

        action_period = f"Next {minutes:g} mins - As of {prefix} {local_start:%H:%M}"
        scenarios = self.report.scenarios if self.report is not None and self.report.scenarios is not None else self.config.energy_plan.scenarios
        if self.chart is not None:
            self.action_period_label.set_text(action_period)
            if hasattr(self, "demonstration_label"):
                self.demonstration_label.set_visibility(self.demonstration)
            self.action_label.set_text(description)
            for control, dimension in ((self.solar_scenario, scenarios.solar), (self.load_scenario, scenarios.house_load)):
                control.options = {name: f"{name.title()} ({level.multiplier:g}x)" for name, level in dimension.items()}
                control.update()
            self._render_scenario()
            return
        with ui.column().classes("w-full gap-1 rounded-lg border border-blue-100 bg-blue-50 px-4 py-3").props("data-testid=plan-action"):
            with ui.row().classes("items-baseline gap-1 flex-wrap"):
                self.action_period_label = ui.label(action_period).classes("text-xs text-blue-800")
                self.demonstration_label = ui.label("[Demonstration - not part of actual plan, save config to disk to apply change]").classes("text-xs text-red-700")
                self.demonstration_label.set_visibility(self.demonstration)
            self.action_label = ui.label(description).classes("text-base font-medium text-blue-950")
        with ui.row().classes("w-full items-center justify-end flex-wrap gap-2"):
            with ui.row().classes("items-center flex-wrap gap-6"):
                for label, dimension in (("Solar", scenarios.solar), ("Load", scenarios.house_load)):
                    with ui.element("fieldset").classes("plan-scenario-group").props(f"data-testid=plan-{label.lower()}-scenario"):
                        ui.html(label, tag="legend").classes("text-sm font-semibold")
                        control = ui.toggle(
                            {name: f"{name.title()} ({level.multiplier:g}x)"
                             for name, level in dimension.items()},
                            value="med", on_change=self._render_scenario,
                        ).props(f'dense no-caps unelevated color=grey-2 text-color=grey-9 toggle-color=primary toggle-text-color=white aria-label="{label} scenario"')
                    if label == "Solar":
                        self.solar_scenario = control
                    else:
                        self.load_scenario = control
        self.summary_output = ui.column().classes("w-full min-w-0 gap-3")
        with ui.element("div").classes("relative w-full"):
            self.chart = ui.echart(plan_chart_options(self._scenario_frame(), self.actuals, self.config.timezone, self.forecast_chart_type)).classes("w-full h-96").props("data-testid=plan-chart")
            ui.toggle({"line": "Line", "bar": "Bar"}, value=self.forecast_chart_type, on_change=self._forecast_chart_type_changed).props('dense no-caps aria-label="Power chart type" data-testid=plan-forecast-chart-type').classes("absolute right-0 top-0 z-10")
        self.scenario_output = ui.column().classes("w-full min-w-0 gap-3")
        self._render_scenario()

    @property
    def scenario_name(self) -> str:
        load, solar = self.load_scenario.value, self.solar_scenario.value
        return "med" if load == solar == "med" else f"{load}_{solar}"

    def _scenario_frame(self) -> pd.DataFrame:
        frame = self.result.results[self.scenario_name]
        extra = self.result.lookahead_results.get(self.scenario_name)
        if self.detail_view == "Advanced" and extra is not None and not extra.empty:
            return pd.concat([frame, extra], ignore_index=True)
        return frame

    def _render_scenario(self) -> None:
        frame = self.result.results[self.scenario_name]
        capacity = self.report.command_battery_capacity_kwh if self.report is not None else None
        solar_export = (self.report.action_solar_export or {}).get(self.scenario_name, frame.solar_to_grid_kwh.iloc[0] if "solar_to_grid_kwh" in frame else 0.)
        self.action_label.set_text(next_action_description(self.result.next_action, solar_export, capacity))
        reserve = self.report.preferred_reserve_kwh if self.report is not None and self.report.preferred_reserve_kwh is not None else self.config.energy_plan.preferred_reserve_kwh
        summary = (self.report.summaries or {}).get(self.scenario_name) or scenario_summary(frame, reserve)
        cost_text = scenario_cost_summary(self.result.results, self.scenario_name)
        series = plan_chart_options(self._scenario_frame(), self.actuals, self.config.timezone, self.forecast_chart_type)["series"]
        self.chart.options["series"] = series
        self.chart.run_chart_method("setOption", {"series": series}, {"replaceMerge": ["series"]})
        self.summary_output.clear()
        with self.summary_output:
            if self.result.expected_score is not None:
                ui.label(expected_plan_summary(self.result, self.report.expected_visible_net_cost)).classes("text-sm font-semibold")
            with ui.element("div").classes("grid w-full grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 rounded border"):
                for label, value in (
                    ("Stress-case net cost" if self.result.expected_score is not None else "Net cost", cost_text),
                    ("Solar / House Load", f"{summary['solar']:.2f} / {summary['load']:.2f} kWh"),
                    ("Grid import / export", f"{summary['import']:.2f} / {summary['export']:.2f} kWh"),
                    ("Minimum / ending battery", f"{summary['minimum']:.2f} / {summary['ending']:.2f} kWh"),
                    ("Time below target reserve", f"{summary['below_reserve']:.2f} hours"),
                    ("Average protection", f"{summary['protection']:.1f} / 100"),
                ):
                    with ui.row().classes("min-w-0 items-baseline justify-between gap-x-2 px-3 py-2"):
                        ui.label(label).classes("text-xs text-gray-600")
                        ui.label(value).classes("text-sm font-semibold")
        self._render_detail()

    def _forecast_chart_type_changed(self, event: ValueChangeEventArguments) -> None:
        if event.value not in ("line", "bar"):
            return
        self.forecast_chart_type = event.value
        series = plan_chart_options(self._scenario_frame(), self.actuals, self.config.timezone, self.forecast_chart_type)["series"]
        self.chart.options["series"] = series
        self.chart.run_chart_method("setOption", {"series": series}, {"replaceMerge": ["series"]})

    def _detail_view_changed(self, event: ValueChangeEventArguments) -> None:
        self.detail_view = event.value
        self._render_scenario()

    def _render_detail(self) -> None:
        if self.report is not None and self.report.details is not None:
            frame = self.report.details[self.detail_view][self.scenario_name].copy()
        else:
            frame = detail_frame(self._scenario_frame(), self.battery_capacity_kwh)
            if self.detail_view == "Compact":
                frame["losses_kwh"] += frame["solar_clipped_kwh"]
                frame = hourly_detail_frame(frame, self.config.timezone, self.result.next_action)
            else:
                frame = advanced_plan_frame(frame, self.config.timezone, self.result.next_action)
        self.scenario_output.clear()
        with self.scenario_output:
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                ui.label("Plan detail").classes("text-lg font-semibold")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    views = ["Compact", "Advanced"] if self.report.full_resolution else ["Compact"]
                    ui.toggle(views, value=self.detail_view, on_change=self._detail_view_changed).props("dense no-caps")
                    ui.button("Download Excel", icon="download", on_click=lambda: self._download_detail(frame, columns)).props("flat dense")
            if self.detail_view == "Advanced":
                ui.label("Flow key: S = Solar · B = Battery · L = Household load · G = Grid. Arrows show energy direction, e.g. S→B is Solar to Battery.").classes("text-xs text-gray-600")
            if not self.report.full_resolution:
                ui.label("Advanced details have expired. This snapshot retains Compact data and an hourly forecast chart.").classes("text-xs text-gray-600")
            columns = render_detail_table(frame, self.config.timezone, self.result.next_action.mode, self.detail_view)

    def _download_detail(self, frame: pd.DataFrame, columns: list[dict[str, Any]]) -> None:
        try:
            basis = "Fixed-condition stress case; not a Markov expectation" if self.result.expected_score is not None else None
            content = detail_excel(frame, self.config.timezone, self.result.next_action.mode, self.detail_view, report_basis=basis)
            start = pd.Timestamp(frame.starts_at.iloc[0]).tz_convert(self.timezone)
            view = "com" if self.detail_view == "Compact" else "adv"
            ui.download(content, filename=f"plan-{self.applied_model}-{view}-protection-{self.applied_adjustment:g}-solar-{self.solar_scenario.value}-load-{self.load_scenario.value}-{start:%Y%m%d-%H%M}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as error:
            LOGGER.exception("Plan detail download failed")
            notify_error(f"Unable to download plan detail: {error}")
