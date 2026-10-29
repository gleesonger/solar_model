"""Per-browser energy-plan controls and presentation."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from enum import StrEnum
from html import escape
import json
import math
from zoneinfo import ZoneInfo
from typing import Literal

from nicegui import run, ui
import pandas as pd

from solar_model.config import Config
from solar_model.dashboard.components.warnings import notify_error
from solar_model.dashboard.planning_worker import ensure_planning_worker
from solar_model.dashboard.components.tables import mark_final_total_row
from solar_model.dashboard.components.download_dialog import excel_bytes
from solar_model.logging_setup import LOGGER
from solar_model.planning.optimisation import battery_protection_score
from solar_model.planning.scenarios import LOAD_SCENARIOS, SOLAR_SCENARIOS
from solar_model.planning.types import OperatingMode, SimulationResult
from solar_model.services.planning import plan_with_preference
from solar_model.utils import table_to_object_list


class Aggregation(StrEnum):
    QUANTITY = "quantity"
    AVERAGE = "average"
    OPENING_STATE = "opening_state"
    CLOSING_STATE = "closing_state"
    SETTING = "setting"
    TEXT = "text"
    START_TIME = "start_time"
    END_TIME = "end_time"
    DURATION = "duration"


@dataclass(frozen=True)
class PlanColumn:
    name: str
    label: str
    compact_label: str | None
    compact_order: int | None
    format: Literal["time", "text", "number"]
    decimals: int | None
    aggregation: Aggregation
    per_hour: bool
    export: bool


# Insertion order is Advanced order; Compact has its own explicit positions.
PLAN_COLUMNS = {column.name: column for column in table_to_object_list(PlanColumn,
    #name                                    label                                   compact_label    compact_order format    decimals aggregation                per_hour export
    ("starts_at",                            "Start",                                None,            1,            "time",   None,    Aggregation.START_TIME,    False,   True),
    ("ends_at",                              "End",                                  None,            2,            "time",   None,    Aggregation.END_TIME,      False,   True),
    ("t",                                    "t",                                    None,            3,            "number", 2,       Aggregation.DURATION,      False,   False),
    ("plan_action",                          "Plan action",                          "Action",        4,            "text",   None,    Aggregation.TEXT,          False,   True),
    ("tariff_name",                          "Tariff Name",                          "Tariff",        5,            "text",   None,    Aggregation.TEXT,          False,   True),
    ("import_rate",                          "Import rate (€/kWh)",                  None,            6,            "number", 4,       Aggregation.AVERAGE,       False,   True),
    ("export_rate",                          "Export rate (€/kWh)",                  None,            7,            "number", 4,       Aggregation.AVERAGE,       False,   True),
    ("load_kwh",                             "House Load (kWh)",                     "House (kWh)",   8,            "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_kwh",                            "Solar kWh",                            "Solar (kWh)",   9,            "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_load_kwh",                    "Solar To Load kWh",                    None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_battery_kwh",                 "Solar To Battery kWh",                 None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_battery_loss_kwh",            "Solar To Battery Loss kWh",            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_grid_kwh",                    "Solar To Grid kWh",                    None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_clipped_kwh",                    "Solar Clipped kWh",                    None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_inverter_kwh",                "Solar To Inverter kWh",                None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_to_load_kwh",                     "Grid To Load kWh",                     None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_to_battery_kwh",                  "Grid To Battery kWh",                  None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_to_battery_loss_kwh",             "Grid To Battery Loss kWh",             None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_ac_to_dc_kwh",                "Inverter Ac To Dc kWh",                None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_load_kwh",                  "Battery To Load kWh",                  None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_grid_kwh",                  "Battery To Grid kWh",                  None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_ac_loss_kwh",               "Battery To Ac Loss kWh",               None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_inverter_kwh",              "Battery To Inverter kWh",              None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_dc_to_ac_kwh",                "Inverter Dc To Ac kWh",                None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("losses_kwh",                           "Losses kWh",                           "Losses (kWh)",  10,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_import_kwh",                      "Grid Import kWh",                      "Import (kWh)",  11,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_export_kwh",                      "Grid Export kWh",                      "Export (kWh)",  12,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("minimum_battery_kwh",                  "Minimum Battery kWh",                  None,            None,         "number", 2,       Aggregation.SETTING,       False,   True),
    ("maximum_battery_kwh",                  "Maximum Battery kWh",                  None,            None,         "number", 2,       Aggregation.SETTING,       False,   True),
    ("limit_battery_export_rate_to_grid_kw", "Limit Battery Export Rate To Grid kW", None,            None,         "number", 2,       Aggregation.SETTING,       False,   True),
    ("battery_start_kwh",                    "Battery start (kWh)",                  None,            None,         "number", 2,       Aggregation.OPENING_STATE, False,   True),
    ("battery_start_soc_percent",            "Battery start SOC (%)",                None,            None,         "number", 1,       Aggregation.OPENING_STATE, False,   True),
    ("battery_charging",                     "Battery Charging",                     None,            None,         "number", 2,       Aggregation.SETTING,       False,   True),
    ("battery_end_kwh",                      "Battery end (kWh)",                    "Battery (kWh)", 13,           "number", 2,       Aggregation.CLOSING_STATE, False,   True),
    ("battery_end_soc_percent",              "Battery end SOC (%)",                  "SOC (%)",       14,           "number", 1,       Aggregation.CLOSING_STATE, False,   True),
    ("import_cost",                          "Import cost (€)",                      "Cost (€)",      15,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("export_revenue",                       "Export revenue (€)",                   "Revenue (€)",   16,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("net_cost",                             "Net cost (€)",                         "Net (€)",       17,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("cost_score",                           "Cost Score",                           None,            None,         "number", 2,       Aggregation.QUANTITY,      True,    True),
    ("protection_score",                     "Protection Score",                     None,            None,         "number", 2,       Aggregation.QUANTITY,      True,    True),
    ("terminal_score",                       "Terminal Score",                       None,            None,         "number", 2,       Aggregation.QUANTITY,      True,    True),
    ("total_score",                          "Total Score",                          None,            None,         "number", 2,       Aggregation.QUANTITY,      True,    True),
)}


def detail_column(name: str) -> PlanColumn:
    """Retain inferred presentation/aggregation for unlisted Advanced columns."""
    if name in PLAN_COLUMNS:
        return PLAN_COLUMNS[name]
    label = name.replace("_", " ").title().replace("Kwh", "kWh").replace("Kw", "kW")
    return PlanColumn(name, label, None, None, "number", 1 if name.endswith("soc_percent") else 2,
                      Aggregation.QUANTITY if name.endswith("_kwh") else Aggregation.SETTING, False, True)


def detail_columns(frame: pd.DataFrame, compact: bool) -> list[dict]:
    """Build table/export headers from the shared definitions."""
    available = {"t", "plan_action", *frame.columns}
    definitions = [column for column in PLAN_COLUMNS.values() if column.name in available]
    if compact:
        definitions = sorted((column for column in definitions if column.compact_order is not None),
                             key=lambda column: column.compact_order)
    else:
        definitions += [detail_column(name) for name in frame if name not in PLAN_COLUMNS]
    columns = []
    for definition in definitions:
        column = {"name": definition.name, "field": definition.name,
                  "label": (definition.compact_label or definition.label) if compact else definition.label,
                  "align": "right" if definition.format == "number" else "left", "sortable": False}
        if definition.decimals is not None:
            column[":format"] = f"value => value == null ? '' : Number(value).toFixed({definition.decimals})"
        columns.append(column)
    return columns


def scenario_summary(frame: pd.DataFrame, reserve: float) -> dict[str, float]:
    """Summarise interval energy/costs and duration-weighted retained protection."""
    durations = (pd.to_datetime(frame.ends_at) - pd.to_datetime(frame.starts_at)).dt.total_seconds() / 3600
    levels = frame.battery_end_kwh.astype(float)
    protection = levels.map(lambda energy: battery_protection_score(max(0., energy), reserve))
    return {
        "cost": float(frame.net_cost.sum()),
        "solar": float(frame.solar_kwh.sum()),
        "load": float(frame.load_kwh.sum()),
        "import": float(frame.grid_import_kwh.sum()),
        "export": float(frame.grid_export_kwh.sum()),
        "minimum": float(min(frame.battery_start_kwh.min(), levels.min())),
        "ending": float(levels.iloc[-1]),
        "below_reserve": float(durations[levels < reserve].sum()),
        "protection": float((protection * durations).sum() / durations.sum()),
    }


def table_detail_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Show hourly score rates and elapsed-hour weights without changing exports."""
    display = frame.copy()
    hours = (pd.to_datetime(frame.ends_at) - pd.to_datetime(frame.starts_at)).dt.total_seconds() / 3600
    display["t"] = hours
    for column in frame:
        if detail_column(column).per_hour:
            display[column] = frame[column] / hours
    return display


def detail_frame(frame: pd.DataFrame, capacity: float | None) -> pd.DataFrame:
    """Add stored-energy SOC beside each battery level, without changing results."""
    frame = frame.copy()
    for position in ("start", "end"):
        level = f"battery_{position}_kwh"
        frame.insert(frame.columns.get_loc(level) + 1, f"battery_{position}_soc_percent",
                     frame[level].astype(float) / capacity * 100 if capacity and capacity > 0 else float("nan"))
    losses = [column for column in frame.columns if column.endswith("_loss_kwh")]
    frame["losses_kwh"] = frame[losses].sum(axis=1, min_count=1)
    return frame


def detail_rows(frame: pd.DataFrame, timezone_name: str, first_mode: OperatingMode | None = None) -> list[dict]:
    """Keep numeric cells sortable and display local interval times as HH:MM."""
    display = frame.copy()
    if "plan_action" not in display:
        display["plan_action"] = [
            "Force charge" if row.get("grid_to_battery_kwh", 0) > 1e-6 else
            "Force discharge" if row.get("battery_to_grid_kwh", 0) > 1e-6 else "Self-consumption"
            for row in frame.to_dict("records")
        ]
    if first_mode is not None and not display.empty and "plan_action" not in frame:
        display.iloc[0, display.columns.get_loc("plan_action")] = {
            OperatingMode.CHARGE_PV_FIRST: "Force charge",
            OperatingMode.DISCHARGE_PV_FIRST: "Force discharge",
            OperatingMode.SELF_CONSUMPTION: "Self-consumption",
        }[first_mode]
    for column in display:
        definition = detail_column(column)
        if definition.format == "time":
            timestamps = pd.to_datetime(display[column], utc=True)
            display[column] = timestamps.dt.tz_convert(timezone_name).dt.strftime("%H:%M")
        elif definition.format == "number":
            display[column] = pd.to_numeric(display[column]).round(4)
    display = display.astype(object).where(pd.notna(display), None)
    return [{"period": index + 1, **row} for index, row in enumerate(display.to_dict("records"))]


def detail_totals(frame: pd.DataFrame) -> dict:
    """Sum interval energy/costs; leave state levels, limits and rates blank."""
    totals = {column: None for column in frame.columns}
    for column in frame.columns:
        aggregation = detail_column(column).aggregation
        if aggregation == Aggregation.START_TIME:
            totals[column] = "Total"
        elif aggregation in (Aggregation.QUANTITY, Aggregation.DURATION):
            value = pd.to_numeric(frame[column]).sum(min_count=1)
            totals[column] = round(float(value), 4) if pd.notna(value) else None
    return totals


def hourly_detail_frame(frame: pd.DataFrame, timezone_name: str, first_mode: OperatingMode) -> pd.DataFrame:
    """Split overlapping periods into local clock hours, then sum flows/costs."""
    definitions = {name: detail_column(name) for name in [*frame.columns, "plan_action"]}
    actions = detail_rows(frame, timezone_name, first_mode)
    pieces = []
    for row, action in zip(frame.to_dict("records"), actions):
        start, end = pd.Timestamp(row["starts_at"]), pd.Timestamp(row["ends_at"])
        cursor = start
        while cursor < end:
            hour = cursor.tz_convert(ZoneInfo(timezone_name)).replace(minute=0, second=0, microsecond=0).tz_convert("UTC")
            stop = min(end, hour + pd.Timedelta(hours=1))
            fraction = (stop - cursor) / (end - start)
            piece = {**row, "starts_at": cursor, "ends_at": stop, "plan_action": action["plan_action"],
                     "_hour": hour, "_seconds": (stop - cursor).total_seconds()}
            for column, definition in definitions.items():
                if definition.aggregation == Aggregation.QUANTITY:
                    piece[column] = row[column] * fraction
                elif definition.aggregation == Aggregation.DURATION:
                    piece[column] = (stop - cursor).total_seconds() / 3600
                elif definition.aggregation in (Aggregation.OPENING_STATE, Aggregation.CLOSING_STATE):
                    # Paired state fields use the existing start/end naming convention.
                    first = column.replace("_end_", "_start_")
                    last = column.replace("_start_", "_end_")
                    at = cursor if definition.aggregation == Aggregation.OPENING_STATE else stop
                    piece[column] = row[first] + (row[last] - row[first]) * ((at - start) / (end - start))
            pieces.append(piece)
            cursor = stop
    grouped = []
    for _, group in pd.DataFrame(pieces).groupby("_hour", sort=True):
        row = group.iloc[-1].to_dict()
        row["starts_at"], row["ends_at"] = group.starts_at.iloc[0], group.ends_at.iloc[-1]
        for column, definition in definitions.items():
            aggregation = definition.aggregation
            if aggregation in (Aggregation.QUANTITY, Aggregation.DURATION):
                row[column] = group[column].sum(min_count=1)
            elif aggregation in (Aggregation.OPENING_STATE, Aggregation.START_TIME):
                row[column] = group[column].iloc[0]
            elif aggregation == Aggregation.TEXT:
                row[column] = " / ".join(dict.fromkeys(group[column].dropna()))
            elif aggregation == Aggregation.AVERAGE:
                valid = group[column].notna()
                weights = group.loc[valid, "_seconds"]
                row[column] = (group.loc[valid, column] * weights).sum() / weights.sum() if valid.any() else float("nan")
        grouped.append(row)
    return pd.DataFrame(grouped).drop(columns=["_hour", "_seconds"])


def detail_decimal_places(column: str) -> int | None:
    return detail_column(column).decimals


def plan_chart_options(frame: pd.DataFrame, actuals: pd.DataFrame, timezone_name: str) -> dict:
    """One legend entry per metric, with solid actuals and dashed scenario forecasts."""
    series = []
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
            forecast = [[starts[0], float(frame.battery_start_kwh.iloc[0])]] + list(zip(ends, frame.battery_end_kwh.astype(float)))
        else:
            energy_column = {"solar": "solar_kwh", "house_load": "load_kwh"}.get(column, column + "_kwh")
            values = (frame[energy_column].astype(float) / hours).tolist()
            forecast = list(zip(starts, values)) + [[ends[-1], values[-1]]]
        for kind, data in (("actual", observed), ("forecast", forecast)):
            stepped = kind == "forecast" and column in {"grid_import", "grid_export"}
            series.append({
                "id": f"{column}_{kind}",
                "name": label, "type": "line", "data": data, "yAxisIndex": axis,
                "showSymbol": False, "connectNulls": False, "color": color,
                "step": "end" if stepped else False,
                "smooth": 0.2 if kind == "forecast" and not stepped else False,
                "smoothMonotone": "x",
                "lineStyle": {"type": "solid" if kind == "actual" else "dashed", "width": 2},
            })
    date_format = 'new Intl.DateTimeFormat("en-IE", {timeZone: ' + json.dumps(timezone_name) + ', day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"})'
    return {
        "animation": False,
        "useUTC": True,
        "legend": {"data": ["Solar", "House Load", "Battery", "Grid Import", "Grid Export"], "top": 5},
        "grid": {"left": 55, "right": 60, "top": 85, "bottom": 70},
        "xAxis": {"type": "time", "splitNumber": 4, "axisLabel": {"hideOverlap": True, ":formatter": f"value => {date_format}.format(value).replace(', ', '\\n')"}},
        "yAxis": [{"type": "value", "name": "Power (kW)"}, {"type": "value", "name": "Battery (kWh)", "alignTicks": True}],
        "tooltip": {"trigger": "axis", ":formatter": "params => { if (!params.length) return ''; return " + date_format + ".format(params[0].value[0]) + params.map(p => '<br/>' + p.marker + p.seriesName + (p.seriesIndex % 2 ? ' (forecast): ' : ' (actual): ') + (p.value[1] == null ? '—' : Number(p.value[1]).toFixed(2))).join(''); }"},
        "dataZoom": [{"type": "inside"}, {"type": "slider", "bottom": 10}],
        "series": series,
    }


class PlanTab:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        self.result: SimulationResult | None = None
        self.actuals = pd.DataFrame()
        self.running = False
        self.battery_capacity_kwh: float | None = None
        self.detail_view = "Compact"
        self.applied_adjustment: float | None = None

    def render_plan_tab(self) -> None:
        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
            self.adjustment = ui.number(
                "Protection adjustment", value=self.config.energy_plan.protection_preference,
                min=0, step=1, on_change=self._adjustment_changed,
            ).props("outlined dense").classes("w-64 max-w-full")
            self.adjustment.tooltip(f"Higher values give battery protection more value relative to net cost. 0 considers cost only. Target reserve: {self.config.energy_plan.preferred_reserve_kwh:g} kWh.")
            self.refresh_button = ui.button("Refresh plan", icon="refresh", on_click=self.refresh)
            with ui.row().classes("items-center gap-2") as self.loading:
                ui.spinner(size="sm")
                ui.label("Calculating plan…").classes("text-sm text-gray-600")
            self.loading.set_visibility(False)
        self.status = ui.label("Refresh to calculate a plan from the latest available data.").classes("text-sm text-gray-600")
        self.output = ui.column().classes("w-full min-w-0 gap-4")

    def _adjustment_changed(self) -> None:
        if not self.running:
            self.refresh_button.set_enabled(self.applied_adjustment is None or self.adjustment.value != self.applied_adjustment)

    async def refresh(self) -> None:
        if self.running:
            return
        try:
            value = self.adjustment.value
            if value is None or not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError("Protection adjustment must be finite and non-negative")
            preference = float(value)
        except (ValueError, TypeError) as error:
            notify_error(str(error))
            return
        self.running = True
        self.refresh_button.set_visibility(False)
        self.loading.set_visibility(True)
        self.adjustment.disable()
        self.status.set_visibility(False)
        as_of = datetime.now(timezone.utc).replace(microsecond=0)
        succeeded = False
        try:
            await ensure_planning_worker()
            report = await run.cpu_bound(plan_with_preference, self.config, as_of, preference)
            if report is None:
                raise RuntimeError("The plan calculation did not return a result")
            self.result, self.actuals = report.simulation, report.actuals
            self.battery_capacity_kwh = report.battery_capacity_kwh
            self.output.clear()
            with self.output:
                self._render_result()
            self.applied_adjustment = preference
            succeeded = True
        except Exception as error:
            LOGGER.exception("Dashboard plan refresh failed")
            previous = " The previous plan is still shown." if self.result is not None else ""
            self.status.set_text("Plan refresh failed." + previous)
            self.status.set_visibility(True)
            notify_error(f"Unable to calculate plan: {error}")
        finally:
            self.running = False
            self.loading.set_visibility(False)
            self.refresh_button.set_visibility(True)
            self.refresh_button.set_enabled(not succeeded)
            self.adjustment.enable()

    def _render_result(self) -> None:
        result = self.result
        first = next(iter(result.results.values()))
        action = result.next_action
        minutes = (first.ends_at.iloc[0] - first.starts_at.iloc[0]).total_seconds() / 60
        if action.mode == OperatingMode.CHARGE_PV_FIRST:
            description = f"Force charge at {action.force_battery_charge_rate:.2f} kW until {action.charge_cut_off_soc_percent:.1f}% SOC"
        elif action.mode == OperatingMode.DISCHARGE_PV_FIRST:
            description = f"Force discharge at {action.force_battery_discharge_rate:.2f} kW until {action.discharge_cut_off_soc_percent:.1f}% SOC"
        else:
            description = "Self-consumption"
        ui.label(f"Action for next {minutes:g} mins - {description}").classes("text-base font-semibold")
        costs = [float(frame.net_cost.sum()) for frame in result.results.values()]
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
            ui.label(f"Scenario-average net cost: €{sum(costs) / len(costs):.2f} · Range €{min(costs):.2f} to €{max(costs):.2f}").classes("text-base font-semibold")
            with ui.row().classes("items-center flex-wrap gap-6"):
                for label, scenarios in (("Solar", SOLAR_SCENARIOS), ("Load", LOAD_SCENARIOS)):
                    with ui.element("fieldset").classes("plan-scenario-group").props(f"data-testid=plan-{label.lower()}-scenario"):
                        ui.html(label, tag="legend").classes("text-sm font-semibold")
                        control = ui.toggle(
                            {name: "Base" if name == "base" else f"{name.title()} ({factor:g}x)"
                             for name, (factor, _) in scenarios.items()},
                            value="base", on_change=self._render_scenario,
                        ).props(f'dense no-caps unelevated color=grey-2 text-color=grey-9 toggle-color=primary toggle-text-color=white aria-label="{label} scenario"')
                    if label == "Solar":
                        self.solar_scenario = control
                    else:
                        self.load_scenario = control
        self.summary_output = ui.column().classes("w-full min-w-0 gap-3")
        if self.actuals.empty:
            ui.label("No actual measurements available for today.").classes("text-sm text-gray-600")
        self.chart = ui.echart(plan_chart_options(self._scenario_frame(), self.actuals, self.config.timezone)).classes("w-full h-96").props("data-testid=plan-chart")
        self.scenario_output = ui.column().classes("w-full min-w-0 gap-3")
        self._render_scenario()

    @property
    def scenario_name(self) -> str:
        load, solar = self.load_scenario.value, self.solar_scenario.value
        return "base" if load == solar == "base" else f"{load}_{solar}"

    def _scenario_frame(self) -> pd.DataFrame:
        frame = self.result.results[self.scenario_name]
        extra = self.result.lookahead_results.get(self.scenario_name)
        if self.detail_view == "Advanced" and extra is not None and not extra.empty:
            return pd.concat([frame, extra], ignore_index=True)
        return frame

    def _render_scenario(self) -> None:
        frame = self.result.results[self.scenario_name]
        summary = scenario_summary(frame, self.config.energy_plan.preferred_reserve_kwh)
        series = plan_chart_options(self._scenario_frame(), self.actuals, self.config.timezone)["series"]
        self.chart.options["series"] = series
        self.chart.run_chart_method("setOption", {"series": series}, {"replaceMerge": ["series"]})
        self.summary_output.clear()
        with self.summary_output:
            with ui.element("div").classes("grid w-full grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 rounded border"):
                for label, value in (
                    ("Net cost", f"€{summary['cost']:.2f}"),
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

    def _detail_view_changed(self, event) -> None:
        self.detail_view = event.value
        self._render_scenario()

    def _render_detail(self) -> None:
        frame = detail_frame(self._scenario_frame(), self.battery_capacity_kwh)
        if self.detail_view == "Compact":
            frame["losses_kwh"] += frame["solar_clipped_kwh"]
            frame = hourly_detail_frame(frame, self.config.timezone, self.result.next_action.mode)
        self.scenario_output.clear()
        with self.scenario_output:
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                ui.label("Plan detail").classes("text-lg font-semibold")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.toggle(["Compact", "Advanced"], value=self.detail_view, on_change=self._detail_view_changed).props("dense no-caps")
                    ui.button("Download Excel", icon="download", on_click=lambda: self._download_detail(frame, columns)).props("flat dense")
            columns = detail_columns(frame, compact=self.detail_view == "Compact")
            display = table_detail_frame(frame)
            table = ui.table(columns=columns, rows=detail_rows(display, self.config.timezone, self.result.next_action.mode), row_key="period", pagination=None).props("dense flat bordered").classes("w-full max-w-full")
            totals = detail_totals(frame.assign(t=display.t))
            for column in columns:
                name = column["name"]
                if totals.get(name) is not None and detail_decimal_places(name) is not None:
                    totals[name] = f"{totals[name]:.{detail_decimal_places(name)}f}"
            table.add_slot("bottom-row", "<q-tr>" + "".join(
                f'<q-td class="text-{column["align"]}">{escape(str(totals[column["name"]])) if totals.get(column["name"]) is not None else ""}</q-td>'
                for column in columns
            ) + "</q-tr>")
            mark_final_total_row(table).classes("plan-detail-table")
            if self.detail_view == "Advanced":
                table.classes("plan-detail-advanced")

    def _download_detail(self, frame: pd.DataFrame, columns: list[dict]) -> None:
        try:
            columns = [column for column in columns if detail_column(column["name"]).export]
            rows = detail_rows(frame, self.config.timezone, self.result.next_action.mode)
            exported = pd.DataFrame([*rows, detail_totals(frame)])
            exported = exported.reindex(columns=[column["name"] for column in columns])
            exported = exported.rename(columns={column["name"]: column["label"] for column in columns})
            content = excel_bytes(exported, sheet_name="Plan detail")
            start = pd.Timestamp(frame.starts_at.iloc[0]).tz_convert(self.timezone)
            ui.download(content, filename=f"plan-{self.scenario_name}-{start:%Y%m%d-%H%M}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as error:
            LOGGER.exception("Plan detail download failed")
            notify_error(f"Unable to download plan detail: {error}")
