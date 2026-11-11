"""Shared, UI-independent plan detail transformations and column contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from solar_model.config import ScenariosConfig
from solar_model.planning.plan_optimisation_milp import battery_protection_score
from solar_model.planning.scenarios import scenario_probabilities
from solar_model.planning.types import NextAction, OperatingMode, SimulationResult
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
    ("t",                                    "hours",                                None,            3,            "number", 2,       Aggregation.DURATION,      False,   False),
    ("plan_action",                          "Action",                               "Action",        4,            "text",   None,    Aggregation.TEXT,          False,   True),
    ("tariff_name",                          "Tariff",                               "Tariff",        5,            "text",   None,    Aggregation.TEXT,          False,   True),
    ("import_rate",                          "Import rate (€/kWh)",                  None,            6,            "number", 4,       Aggregation.AVERAGE,       False,   True),
    ("export_rate",                          "Export rate (€/kWh)",                  None,            7,            "number", 4,       Aggregation.AVERAGE,       False,   True),
    ("load_kwh",                             "House Load (kWh)",                     "House (kWh)",   8,            "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_kwh",                            "Solar kWh",                            "Solar (kWh)",   9,            "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_load_kwh",                    "S→L\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_battery_kwh",                 "S→B\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_charge_loss_kwh",              "Battery Charge Loss kWh",              None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_grid_kwh",                    "S→G\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_clipped_kwh",                    "Clipped\nkWh",                        None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("solar_to_inverter_kwh",                "Solar To Inverter kWh",                None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_to_load_kwh",                     "G→L\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_to_battery_kwh",                  "G→B\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_ac_to_dc_loss_kwh",           "Inverter AC To DC Loss kWh",           None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_ac_to_dc_kwh",                "AC→DC\nkWh",                          None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_load_kwh",                  "B→L\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_grid_kwh",                  "B→G\nkWh",                            None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_discharge_loss_kwh",           "Battery Discharge Loss kWh",           None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_dc_to_ac_loss_kwh",           "Inverter DC To AC Loss kWh",           None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("battery_to_inverter_kwh",              "Battery To Inverter kWh",              None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("inverter_dc_to_ac_kwh",                "DC→AC\nkWh",                          None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("losses_kwh",                           "Losses\nkWh",                         "Losses (kWh)",  10,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_import_kwh",                      "Import\nkWh",                         "Import (kWh)",  11,           "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("grid_export_kwh",                      "Export\nkWh",                         "Export (kWh)",  12,           "number", 2,       Aggregation.QUANTITY,      False,   True),
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
    ("limit_score",                          "Limit Score",                          None,            None,         "number", 2,       Aggregation.QUANTITY,      True,    True),
    ("terminal_score",                       "Terminal Score",                       None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
    ("total_score",                          "Total Score",                          None,            None,         "number", 2,       Aggregation.QUANTITY,      False,   True),
)}

ADVANCED_HIDDEN_COLUMNS = {
    "battery_start_kwh",
    "battery_start_soc_percent",
    "solar_to_inverter_kwh",
    "battery_to_inverter_kwh",
    "battery_charge_loss_kwh",
    "battery_discharge_loss_kwh",
    "inverter_ac_to_dc_loss_kwh",
    "inverter_dc_to_ac_loss_kwh",
}

ADVANCED_LABELS = {
    "import_rate": "Import rate\n€/kWh",
    "export_rate": "Export rate\n€/kWh",
    "load_kwh": "Load\nkWh",
    "solar_kwh": "Solar\nkWh",
    "minimum_battery_kwh": "Battery min\nkWh",
    "maximum_battery_kwh": "Battery max\nkWh",
    "limit_battery_export_rate_to_grid_kw": "Battery export\nlimit kW",
    "battery_charging": "Charging",
    "battery_end_kwh": "Battery end\nkWh",
    "battery_end_soc_percent": "Battery end\nSOC %",
    "import_cost": "Import cost\n€",
    "export_revenue": "Export revenue\n€",
    "net_cost": "Net cost\n€",
    "cost_score": "Cost\nscore",
    "protection_score": "Protection\nscore",
    "limit_score": "Limit\nscore",
    "terminal_score": "Terminal\nscore",
    "total_score": "Total\nscore",
}


def detail_column(name: str) -> PlanColumn:
    """Retain inferred presentation/aggregation for unlisted Advanced columns."""
    if name in PLAN_COLUMNS:
        return PLAN_COLUMNS[name]
    label = name.replace("_", " ").title().replace("Kwh", "kWh").replace("Kw", "kW")
    return PlanColumn(name, label, None, None, "number", 1 if name.endswith("soc_percent") else 2,
                      Aggregation.QUANTITY if name.endswith("_kwh") else Aggregation.SETTING, False, True)


def detail_columns(frame: pd.DataFrame, compact: bool) -> list[dict[str, Any]]:
    """Build table/export headers from the shared definitions."""
    available = {"t", "plan_action", *frame.columns}
    definitions = [column for column in PLAN_COLUMNS.values() if column.name in available]
    if compact:
        definitions = sorted((column for column in definitions if column.compact_order is not None),
                             key=lambda column: column.compact_order)
    else:
        definitions = [column for column in definitions if column.name not in ADVANCED_HIDDEN_COLUMNS]
        definitions += [detail_column(name) for name in frame if name not in PLAN_COLUMNS and name not in ADVANCED_HIDDEN_COLUMNS]
    columns = []
    for definition in definitions:
        column = {"name": definition.name, "field": definition.name,
                  "label": (definition.compact_label or definition.label) if compact else ADVANCED_LABELS.get(definition.name, definition.label),
                  "align": "right" if definition.format == "number" else "left", "sortable": False}
        if definition.decimals is not None:
            column[":format"] = f"value => value == null ? '' : Number(value).toFixed({definition.decimals})"
        columns.append(column)
    return columns


def scenario_average_cost(results: dict[str, pd.DataFrame], settings: ScenariosConfig) -> float:
    """Use the same joint probabilities as planning for the displayed mean cost."""
    weights = scenario_probabilities(settings)
    return sum(weights[name] * float(frame.net_cost.sum()) for name, frame in results.items()) / sum(weights[name] for name in results)


def scenario_cost_summary(results: dict[str, pd.DataFrame], selected_scenario: str) -> str:
    """Format the selected scenario visible cost and range across all scenarios."""
    costs = [float(frame.net_cost.sum()) for frame in results.values()]
    values = [(float(results[selected_scenario].net_cost.sum()), 3), (min(costs), 2), (max(costs), 2)]
    selected, minimum, maximum = [f"{'-' if value < 0 else ''}\u20ac{abs(value):.{precision}f}" for value, precision in values]
    return f"{selected} ({minimum} to {maximum})"


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
    """Show hourly rates for period scores; retain terminal and total contributions."""
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


def detail_rows(frame: pd.DataFrame, timezone_name: str, first_mode: OperatingMode | NextAction | None = None, *, time_format: str = "%H:%M") -> list[dict[str, Any]]:
    """Keep numeric cells and format local times for the table or Excel export."""
    display = frame.copy()
    if "plan_action" not in display:
        display["plan_action"] = [
            "Force charge" if row.get("grid_to_battery_kwh", 0) > 1e-6 else
            "Force discharge" if row.get("battery_to_grid_kwh", 0) > 1e-6 else "Self-consumption"
            for row in frame.to_dict("records")
        ]
    mode = first_mode.mode if isinstance(first_mode, NextAction) else first_mode
    if mode is not None and not display.empty and "plan_action" not in frame:
        display.iloc[0, display.columns.get_loc("plan_action")] = {
            OperatingMode.CHARGE_PV_FIRST: "Force charge",
            OperatingMode.DISCHARGE_PV_FIRST: "Force discharge",
            OperatingMode.SELF_CONSUMPTION: "Self-consumption",
        }[mode]
    for index, row in enumerate(frame.to_dict("records")):
        label = display.iloc[index]["plan_action"]
        solar_export = row.get("solar_to_grid_kwh", 0.)
        if label not in ("Force charge", "Force discharge") or pd.isna(solar_export) or solar_export <= 1e-6:
            continue
        if index == 0 and isinstance(first_mode, NextAction):
            rate = first_mode.charge_power_limit_kw if label == "Force charge" else first_mode.discharge_power_limit_kw
        elif label == "Force charge":
            rate = row.get("solar_to_battery_kwh", 0.) + row.get("grid_to_battery_kwh", 0.)
        else:
            rate = row.get("battery_to_inverter_kwh", row.get("battery_to_load_kwh", 0.) + row.get("battery_to_grid_kwh", 0.))
        if pd.notna(rate) and abs(rate) <= 1e-6:
            display.iloc[index, display.columns.get_loc("plan_action")] = "Export Solar Excess"
    for column in display:
        definition = detail_column(column)
        if definition.format == "time":
            timestamps = pd.to_datetime(display[column], utc=True)
            display[column] = timestamps.dt.tz_convert(timezone_name).dt.strftime(time_format)
        elif definition.format == "number":
            display[column] = pd.to_numeric(display[column]).round(4)
    display = display.astype(object).where(pd.notna(display), None)
    return [{"period": index + 1, **row} for index, row in enumerate(display.to_dict("records"))]


def advanced_plan_frame(frame: pd.DataFrame, timezone_name: str, first_mode: OperatingMode | NextAction | None = None) -> pd.DataFrame:
    """Describe projected grid power in the action's direction and battery targets."""
    display = frame.copy()
    actions = []
    for row, formatted in zip(frame.to_dict("records"), detail_rows(frame, timezone_name, first_mode)):
        if formatted["plan_action"] == "Self-consumption":
            actions.append("Self-consumption")
            continue
        parts = []
        hours = (row["ends_at"] - row["starts_at"]).total_seconds() / 3600
        grid_direction = "import" if formatted["plan_action"] == "Force charge" else "export"
        energy = row.get(f"grid_{grid_direction}_kwh")
        if hours > 0 and pd.notna(energy):
            parts.append(f"{grid_direction} {energy / hours:.2f} kW")
        target = row.get("battery_end_kwh")
        if pd.notna(target):
            text = f"{target:.0f}kWh"
            soc = row.get("battery_end_soc_percent")
            if pd.notna(soc):
                text += f"/{soc:.0f}%"
            parts.append(text)
        actions.append(formatted["plan_action"] + (": " + " to ".join(parts) if parts else ""))
    display["plan_action"] = actions
    return display


def next_action_description(action: NextAction, solar_export_kwh: float, capacity: float | None) -> str:
    """Describe the command for the displayed scenario without altering its mode."""
    if action.mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        rate = action.force_battery_charge_rate if action.mode == OperatingMode.CHARGE_PV_FIRST else action.force_battery_discharge_rate
        if rate <= 1e-6 and pd.notna(solar_export_kwh) and solar_export_kwh > 1e-6:
            return "Export Solar Excess"
        description = f"{action.mode.label} at {rate:.2f} kW"
        if action.target_soc_percent is not None:
            soc = action.target_soc_percent
            target = f"{capacity * soc / 100:.2f} kWh ({soc:.1f}%)" if capacity is not None and capacity > 0 else f"{soc:.1f}%"
            description += f"; target {target}"
        return description
    return action.mode.label


def detail_totals(frame: pd.DataFrame) -> dict[str, str | float | None]:
    """Sum interval energy/costs; leave state levels, limits and rates blank."""
    totals: dict[str, str | float | None] = {column: None for column in frame.columns}
    for column in frame.columns:
        aggregation = detail_column(column).aggregation
        if aggregation == Aggregation.START_TIME:
            totals[column] = "Total"
        elif aggregation in (Aggregation.QUANTITY, Aggregation.DURATION):
            value = pd.to_numeric(frame[column]).sum(min_count=1)
            totals[column] = round(float(value), 4) if pd.notna(value) else None
    return totals


def advanced_detail_totals(frame: pd.DataFrame) -> list[dict[str, str | float | None]]:
    """Summarise elapsed hours 0–24, the remaining horizon and the full plan."""
    starts = pd.to_datetime(frame.starts_at, utc=True)
    ends = pd.to_datetime(frame.ends_at, utc=True)
    boundary = starts.iloc[0] + pd.Timedelta(hours=24)

    def subtotal(begin: pd.Timestamp, end: pd.Timestamp, label: str) -> dict[str, str | float | None]:
        overlap = pd.Series([max(0., (min(stop, end) - max(start, begin)).total_seconds()) for start, stop in zip(starts, ends)], index=frame.index)
        fraction = overlap / (ends - starts).dt.total_seconds()
        subset = frame.loc[fraction > 0].copy()
        fraction = fraction.loc[subset.index]
        endpoint = pd.Series([(begin < stop <= end) for stop in ends], index=frame.index).loc[subset.index]
        terminal = pd.to_numeric(subset["terminal_score"], errors="coerce") if "terminal_score" in subset else None
        for column in subset:
            aggregation = detail_column(column).aggregation
            if aggregation == Aggregation.DURATION:
                subset[column] = overlap.loc[subset.index] / 3600
            elif aggregation == Aggregation.QUANTITY:
                values = pd.to_numeric(subset[column], errors="coerce")
                if column == "terminal_score":
                    subset[column] = values * endpoint
                elif column == "total_score" and terminal is not None:
                    subset[column] = (values - terminal) * fraction + terminal * endpoint
                else:
                    subset[column] = values * fraction
        totals = detail_totals(subset)
        totals["starts_at"] = label
        return totals

    first = subtotal(starts.iloc[0], boundary, "0–24h")
    second = subtotal(boundary, ends.iloc[-1], "24–48h" if ends.iloc[-1] <= boundary + pd.Timedelta(hours=24) else "24h+")
    return [first, second, detail_totals(frame)]


def hourly_detail_frame(frame: pd.DataFrame, timezone_name: str, first_mode: OperatingMode | NextAction) -> pd.DataFrame:
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


