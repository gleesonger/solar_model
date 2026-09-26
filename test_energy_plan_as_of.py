"""Create an as-of energy plan workbook without using future database data."""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import inspect, select

from config import Config, load_config
from database import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample, SolarDatabase, create_engine_for_database
from energy_plan_simulation import DeviceInfo, DeviceState, ForecastData, ForecastInterval, simulate_energy_plan

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--database", type=Path, default=Path("solar.db"))
    parser.add_argument("--as-of", required=True, help="planning cut-off as an ISO date-time; system timezone is assumed when omitted")
    parser.add_argument("--output-timestep-min", type=int, default=None, help="output resolution in minutes; must be a multiple of the projection resolution")
    parser.add_argument("--output", type=Path, default=Path("energy_plan.xlsx"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        as_of = as_of.astimezone()
    as_of = as_of.astimezone(ZoneInfo(config.timezone))
    LOGGER.info("Planning as of %s", as_of.isoformat())
    LOGGER.info("Loading database %s", args.database)
    database = SolarDatabase(create_engine_for_database(args.database))
    try:
        Path(args.output).unlink(missing_ok=True)
        
        device_info = load_device_info_as_of(database, config, as_of)
        LOGGER.info("Loaded device metadata")

        device_state = load_battery_state_as_of(database, as_of, device_info)
        LOGGER.info("Loaded battery state")

        forecast = load_forecast_as_of(database, as_of, config)
        LOGGER.info("Loaded forecast data")

        LOGGER.info("Solving energy plan")

        plan = simulate_energy_plan(device_info, device_state, forecast, config, projection_starts_at=as_of)

        output_timestep_mins = args.output_timestep_min or config.energy_plan.projection_resolution_mins

        if output_timestep_mins <= 0:
            raise ValueError("--output-timestep-min must be greater than zero")
        if output_timestep_mins % config.energy_plan.projection_resolution_mins:
            raise ValueError("--output-timestep-min must be a multiple of the projection resolution")

        plan = group_plan(plan, output_timestep_mins)

        LOGGER.info("Grouped plan into %d-minute periods (%d rows)", output_timestep_mins, len(plan))
        write_workbook(plan, args.output, as_of)
    finally:
        database.engine.dispose()
    LOGGER.info("Wrote %s", args.output)


def group_plan(plan: pd.DataFrame, output_timestep_mins: int) -> pd.DataFrame:
    """Aggregate projection rows into clock-aligned output periods."""
    grouped_plan = plan.copy()
    grouped_plan["_output_start"] = pd.to_datetime(grouped_plan["starts_at"]).dt.floor(f"{output_timestep_mins}min")
    sum_columns = {column for column in plan.columns if column.endswith("_kwh") or column in {"import_cost", "export_revenue", "net_cost"}}
    sum_columns -= {"minimum_battery_kwh", "maximum_battery_kwh", "battery_start_kwh", "battery_end_kwh", "limit_battery_export_rate_to_grid_kw"}
    rows = []
    for _, group in grouped_plan.groupby("_output_start", sort=True):
        row = {column: group[column].iloc[0] for column in plan.columns}
        row["starts_at"] = group["starts_at"].min()
        row["ends_at"] = group["ends_at"].max()
        row["minimum_battery_kwh"] = group["minimum_battery_kwh"].max()
        row["maximum_battery_kwh"] = group["maximum_battery_kwh"].min()
        row["limit_battery_export_rate_to_grid_kw"] = group["limit_battery_export_rate_to_grid_kw"].min()
        row["battery_start_kwh"] = group["battery_start_kwh"].iloc[0]
        row["battery_end_kwh"] = group["battery_end_kwh"].iloc[-1]
        for column in sum_columns:
            row[column] = group[column].sum()
        row["tariff_name"] = " / ".join(dict.fromkeys(str(value) for value in group["tariff_name"]))
        for rate_column, volume_column in (("import_rate", "grid_import_kwh"), ("export_rate", "grid_export_kwh")):
            volume = group[volume_column].sum()
            row[rate_column] = (group[rate_column] * group[volume_column]).sum() / volume if volume else group[rate_column].iloc[0]
        rows.append(row)
    return pd.DataFrame(rows, columns=plan.columns)


def interval_energies(rows: list[ForecastSolarSample], panel_ids: tuple[int, ...], timezone: ZoneInfo, source: str) -> dict[datetime, float] | None:
    """Convert cumulative Forecast.Solar Wh values into interval kWh totals."""
    by_panel: dict[int, list[tuple[datetime, float]]] = {}
    for panel_id in panel_ids:
        field = f"panel_{panel_id}_{source}_watt_hours"
        points = [(datetime.fromisoformat(row.forecast_time).astimezone(timezone), getattr(row, field)) for row in rows if getattr(row, field) is not None]
        if not points:
            return None
        by_panel[panel_id] = sorted(points, key=lambda point: point[0])
    totals: dict[datetime, float] = defaultdict(float)
    for points in by_panel.values():
        previous_by_day: dict[datetime.date, float] = {}
        for timestamp, watt_hours in points:
            day = timestamp.date()
            previous = previous_by_day.get(day)
            current = float(watt_hours)
            totals[timestamp] += (current if previous is None else max(0.0, current - previous)) / 1_000
            previous_by_day[day] = current
    return dict(totals)


def load_weighted_profile(database: SolarDatabase, timezone: ZoneInfo, now: datetime, config: Config) -> tuple[dict[tuple[int, int], float], dict[tuple[int, int], int]]:
    """Return local weekday/hour kWh averages using configured history settings."""
    local_now = now.astimezone(timezone)
    today_start = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    start_at = today_start - timedelta(days=config.energy_plan.load_forecast_historical_lookback_window_days)
    with database.sessions() as session:
        samples = list(session.scalars(select(SigenStorModbusSample).where(SigenStorModbusSample.collected_at_utc >= start_at.isoformat()).where(SigenStorModbusSample.collected_at_utc < today_start.isoformat()).where(SigenStorModbusSample.plant_load_total_kwh_period.is_not(None)).order_by(SigenStorModbusSample.collected_at_utc)).all())
    daily_slots: dict[tuple[datetime.date, int], float] = defaultdict(float)
    for sample in samples:
        ends_at = datetime.fromisoformat(sample.collected_at_utc).astimezone(timezone)
        interval_start = ends_at - timedelta(microseconds=1)
        daily_slots[(interval_start.date(), interval_start.hour)] += float(sample.plant_load_total_kwh_period)
    totals: dict[tuple[int, int], float] = defaultdict(float)
    weights: dict[tuple[int, int], float] = defaultdict(float)
    observations: dict[tuple[int, int], int] = defaultdict(int)
    for (day, hour), load_kwh in daily_slots.items():
        age_days = (today_start.date() - day).days
        weight = math.pow(0.5, age_days / config.energy_plan.load_forecast_historical_lookback_halflife_days)
        slot = (day.weekday(), hour)
        totals[slot] += load_kwh * weight
        weights[slot] += weight
        observations[slot] += 1
    return ({slot: totals[slot] / weights[slot] for slot in totals if weights[slot] > 0}, dict(observations))


def load_forecast_as_of(database: SolarDatabase, as_of: datetime, config: Config) -> ForecastData:
    """Build a forecast from the newest snapshot collected by the as-of time."""
    cutoff = int(as_of.astimezone(ZoneInfo("UTC")).timestamp())
    with database.sessions() as session:
        snapshot_at = session.scalar(
            select(ForecastSolarSample.collected_at_utc)
            .where(ForecastSolarSample.collected_at_utc <= cutoff)
            .order_by(ForecastSolarSample.collected_at_utc.desc())
            .limit(1)
        )
        if snapshot_at is None:
            raise ValueError(f"no forecast snapshot was available by {as_of.isoformat()}")
        rows = list(session.scalars(select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == snapshot_at).order_by(ForecastSolarSample.forecast_time)).all())
    LOGGER.info("Using forecast snapshot collected at %s", snapshot_at)

    timezone = ZoneInfo(config.timezone)
    panel_ids = tuple(array.panel_id for array in config.forecast.arrays)
    solar_intervals = interval_energies(rows, panel_ids, timezone, "adj") or interval_energies(rows, panel_ids, timezone, "raw")
    if not solar_intervals:
        raise ValueError("the as-of forecast contains no usable solar values")

    load_profile, _ = load_weighted_profile(database, timezone, as_of, config)
    solar: list[ForecastInterval] = []
    previous_ends_at: datetime | None = None
    for ends_at, solar_kwh in sorted(solar_intervals.items()):
        if ends_at < as_of:
            continue
        starts_at = previous_ends_at or ends_at - timedelta(seconds=config.forecast.interval_seconds)
        solar.append(ForecastInterval(starts_at, ends_at, float(solar_kwh)))
        previous_ends_at = ends_at

    projection_end = as_of + timedelta(hours=config.energy_plan.num_projection_hours)
    if not solar:
        raise ValueError("the as-of forecast contains no future solar intervals")
    if solar[0].starts_at > as_of:
        solar.insert(0, ForecastInterval(as_of, solar[0].starts_at, 0.0))
    if solar[-1].ends_at < projection_end:
        solar.append(ForecastInterval(solar[-1].ends_at, projection_end, 0.0))

    load: list[ForecastInterval] = []
    starts_at = as_of
    while starts_at < projection_end:
        expected_load_kwh = load_profile.get((starts_at.weekday(), starts_at.hour))
        if expected_load_kwh is None:
            raise ValueError(f"no historical load profile exists for {starts_at.isoformat()}")
        load.append(ForecastInterval(starts_at, starts_at + timedelta(hours=1), float(expected_load_kwh)))
        starts_at += timedelta(hours=1)

    return ForecastData(solar=solar, load=load)


def load_battery_state_as_of(database: SolarDatabase, as_of: datetime, device_info: DeviceInfo) -> DeviceState:
    """Load the latest battery state known at the as-of time."""
    cutoff = int(as_of.astimezone(ZoneInfo("UTC")).timestamp())
    with database.sessions() as session:
        sample = session.scalar(
            select(SigenStorModbusSample)
            .where(SigenStorModbusSample.collected_at_utc <= cutoff)
            .order_by(SigenStorModbusSample.collected_at_utc.desc())
            .limit(1)
        )
    if sample is None or sample.plant_battery_soc_percent is None:
        raise ValueError(f"no battery state was available by {as_of.isoformat()}")
    LOGGER.info("Using battery telemetry collected at %s", sample.collected_at_utc)
    battery_kwh = device_info.system_battery_capacity_kwh * float(sample.plant_battery_soc_percent) / 100
    return DeviceState(battery_kwh=battery_kwh)


def load_device_info_as_of(database: SolarDatabase, config: Config, as_of: datetime) -> DeviceInfo:
    """Load device metadata using only records known at the as-of time."""
    has_variable_name = any(column["name"] == "variable_name" for column in inspect(database.engine).get_columns(SigenStorDevice.__tablename__))
    columns = [SigenStorDevice.id, SigenStorDevice.collected_at_utc, SigenStorDevice.variable, SigenStorDevice.value, SigenStorDevice.unit]
    if has_variable_name:
        columns.append(SigenStorDevice.variable_name)

    cutoff = as_of.astimezone(ZoneInfo("UTC"))
    with database.sessions() as session:
        rows = session.execute(select(*columns).order_by(SigenStorDevice.id.desc())).mappings().all()

    latest: dict[str, float | str] = {}
    for row in rows:
        if datetime.fromisoformat(row["collected_at_utc"]) > cutoff:
            continue
        variable_name = row.get("variable_name") or re.sub(r"[^a-z0-9]+", "_", row["variable"].casefold()).strip("_")
        suffix = {"kW": "kw", "kWh": "kwh", "kVA": "kva", "V": "v", "Hz": "hz", "%": "percent"}.get(row["unit"])
        if suffix and not variable_name.endswith(f"_{suffix}"):
            variable_name = f"{variable_name}_{suffix}"
        try:
            value: float | str = float(row["value"])
        except ValueError:
            value = row["value"]
        latest.setdefault(variable_name, value)

    latest.update(asdict(config.additional_device_info))
    missing = [name for name in DeviceInfo.__dataclass_fields__ if name not in latest]
    if missing:
        raise ValueError(f"as-of device information is missing: {', '.join(missing)}")
    return DeviceInfo(**{name: latest[name] for name in DeviceInfo.__dataclass_fields__})


def write_workbook(plan: pd.DataFrame, output: Path, as_of: datetime) -> None:
    """Write the plan with useful Excel formatting and a summary sheet."""
    plan = plan.copy()
    for column in ("starts_at", "ends_at"):
        plan[column] = pd.to_datetime(plan[column]).dt.tz_localize(None)
    summary = pd.DataFrame({"item": ["As of", "Periods", "Total import cost", "Total export revenue", "Total net cost"], "value": [as_of.isoformat(), len(plan), plan["import_cost"].sum(), plan["export_revenue"].sum(), plan["net_cost"].sum()]})
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        plan.to_excel(writer, sheet_name="Plan", index=False)
        workbook = writer.book
        summary_sheet = writer.sheets["Summary"]
        plan_sheet = writer.sheets["Plan"]
        summary_sheet.set_zoom(80)
        plan_sheet.set_zoom(80)
        money_format = workbook.add_format({"num_format": '€#,##0.00'})
        energy_format = workbook.add_format({"num_format": "0.000"})
        summary_sheet.set_column("A:A", 24)
        summary_sheet.set_column("B:B", 24, money_format)
        summary_sheet.set_row(0, None, workbook.add_format({"bold": True, "bg_color": "#D9EAF7"}))
        plan_sheet.freeze_panes(1, 0)
        plan_sheet.add_table(0, 0, len(plan), len(plan.columns) - 1, {"columns": [{"header": column} for column in plan.columns], "style": "Table Style Medium 2"})
        for column_number, column in enumerate(plan.columns):
            width = max(len(column), min(24, int(plan[column].astype(str).str.len().max()) + 2))
            plan_sheet.set_column(column_number, column_number, width)
        for column in plan.columns:
            if column.endswith("_kwh"):
                plan_sheet.set_column(plan.columns.get_loc(column), plan.columns.get_loc(column), 14, energy_format)
            elif column in {"import_rate", "export_rate", "import_cost", "export_revenue", "net_cost"}:
                plan_sheet.set_column(plan.columns.get_loc(column), plan.columns.get_loc(column), 14, money_format)
        plan_sheet.conditional_format(1, plan.columns.get_loc("net_cost"), len(plan), plan.columns.get_loc("net_cost"), {"type": "3_color_scale", "min_color": "#C6EFCE", "mid_color": "#FFEB9C", "max_color": "#FFC7CE"})

if __name__ == "__main__":
    main()
