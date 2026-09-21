"""Prepare read-only inputs for a future SigEnergy charge/discharge plan."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select

from common import configure_logging
from config import Config, load_config
from database import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample, SolarDatabase, create_engine_for_database
from energy_plan_simulation import (
    DeviceInfo,
    DeviceState,
    ForecastFigures,
    InputParameters,
    simulate_energy_plan,
)

LOOKBACK_WEEKS = 6
HALF_LIFE_WEEKS = 3


def main() -> None:
    """Print or save read-only planning inputs; never create/apply a plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, help="optional JSON output file; stdout is used by default")
    parser.add_argument("--simulate", action="store_true", help="include a read-only self-consumption simulation")
    args = parser.parse_args()
    config = load_config(args.config)
    configure_logging(config.logging.level)
    inputs = build_energy_plan_inputs(config)
    if args.simulate:
        current_soc_percent = inputs["battery_state"]["soc_percent"]
        if not isinstance(current_soc_percent, (int, float)):
            raise ValueError("latest battery telemetry does not contain a numeric state of charge")
        device_info = DeviceInfo(**inputs["devices"])
        device_state = DeviceState(
            battery_kwh=device_info.system_battery_capacity_kwh * float(current_soc_percent) / 100,
        )
        inputs["simulation"] = asdict(
            simulate_energy_plan(
                forecast_figures_from_preview(inputs),
                config.tariffs,
                InputParameters(
                    config.energy_plan.battery_target_soc_percent,
                    config.energy_plan.battery_minimum_soc_percent,
                    config.energy_plan.charge_efficiency,
                    config.energy_plan.discharge_efficiency,
                ),
                device_state,
            )
        )
    payload = json.dumps(inputs, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)


def build_energy_plan_inputs(config: Config, now: datetime | None = None) -> dict[str, Any]:
    """Load all inputs needed by a future planner, without changing state."""
    timezone = ZoneInfo(config.timezone)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    engine = create_engine_for_database(config.database.path)
    database = SolarDatabase(engine)
    try:
        snapshot_at, forecast_rows = load_latest_forecast(database)
        panel_ids = tuple(array.panel_id for array in config.forecast.arrays)
        adjusted = interval_energies(forecast_rows, panel_ids, timezone, "adj")
        raw = interval_energies(forecast_rows, panel_ids, timezone, "raw")
        selected_source, solar_intervals = (("adjusted", adjusted) if adjusted is not None else ("raw", raw))
        if solar_intervals is None:
            raise RuntimeError("the latest forecast snapshot contains no usable configured-panel energy values")
        load_profile, observations = load_weighted_profile(database, timezone, current)
        intervals = []
        for ends_at, solar_kwh in sorted(solar_intervals.items()):
            if ends_at < current:
                continue
            interval_start = ends_at - timedelta(microseconds=1)
            slot = (interval_start.weekday(), interval_start.hour)
            expected_load_kwh = load_profile.get(slot)
            intervals.append({
                "interval_ends_at": ends_at.isoformat(),
                "solar_forecast_kwh": round(solar_kwh, 4),
                "expected_load_kwh": None if expected_load_kwh is None else round(expected_load_kwh, 4),
                "net_energy_kwh": None if expected_load_kwh is None else round(solar_kwh - expected_load_kwh, 4),
                "load_profile_weekday": interval_start.strftime("%a"),
                "load_profile_hour": f"{interval_start.hour:02d}:00",
                "load_observation_days": observations.get(slot, 0),
            })
        return {
            "purpose": "read-only inputs for a future SigEnergy energy plan",
            "plan_created_or_applied": False,
            "generated_at": current.isoformat(),
            "timezone": config.timezone,
            "solar_forecast": {"snapshot_collected_at": snapshot_at, "source": selected_source, "raw_available": raw is not None, "adjusted_available": adjusted is not None},
            "load_model": {"lookback_weeks": LOOKBACK_WEEKS, "half_life_weeks": HALF_LIFE_WEEKS, "description": "weighted average of complete historical local-day hourly load totals"},
            "battery_state": load_battery_state(database),
            "devices": asdict(load_device_metadata(database)),
            "intervals": intervals,
        }
    finally:
        engine.dispose()


def forecast_figures_from_preview(preview: dict[str, Any]) -> tuple[ForecastFigures, ...]:
    """Convert the JSON-facing preview payload at the simulation boundary."""
    previous_ends_at: datetime | None = None
    forecasts: list[ForecastFigures] = []
    for interval in preview["intervals"]:
        expected_load_kwh = interval["expected_load_kwh"]
        if expected_load_kwh is None:
            continue
        ends_at = datetime.fromisoformat(interval["interval_ends_at"])
        starts_at = previous_ends_at or ends_at - timedelta(hours=1)
        forecasts.append(ForecastFigures(
            starts_at=starts_at,
            ends_at=ends_at,
            solar_kwh=float(interval["solar_forecast_kwh"]),
            load_kwh=float(expected_load_kwh),
        ))
        previous_ends_at = ends_at
    return tuple(forecasts)


def load_latest_forecast(database: SolarDatabase) -> tuple[str | None, list[ForecastSolarSample]]:
    """Load the newest stored forecast snapshot."""
    with database.sessions() as session:
        snapshot_at = session.scalar(select(ForecastSolarSample.collected_at_utc).order_by(ForecastSolarSample.collected_at_utc.desc()).limit(1))
        if snapshot_at is None:
            return None, []
        rows = list(session.scalars(select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == snapshot_at).order_by(ForecastSolarSample.forecast_time)).all())
    return snapshot_at, rows


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


def load_weighted_profile(database: SolarDatabase, timezone: ZoneInfo, now: datetime) -> tuple[dict[tuple[int, int], float], dict[tuple[int, int], int]]:
    """Return local weekday/hour kWh averages over six weeks (3-week half-life)."""
    local_now = now.astimezone(timezone)
    today_start = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    start_at = today_start - timedelta(weeks=LOOKBACK_WEEKS)
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
        weight = math.pow(0.5, age_days / (HALF_LIFE_WEEKS * 7))
        slot = (day.weekday(), hour)
        totals[slot] += load_kwh * weight
        weights[slot] += weight
        observations[slot] += 1
    return ({slot: totals[slot] / weights[slot] for slot in totals if weights[slot] > 0}, dict(observations))


def load_battery_state(database: SolarDatabase) -> dict[str, float | str | None]:
    """Load latest telemetry useful to a later charge/discharge planner."""
    with database.sessions() as session:
        sample = session.scalar(select(SigenStorModbusSample).order_by(SigenStorModbusSample.collected_at_utc.desc()).limit(1))
    if sample is None or sample.plant_battery_soc_percent is None:
        return {"collected_at": None, "soc_percent": None, "available_discharge_kwh": None}
    return {
        "collected_at": sample.collected_at_utc,
        "soc_percent": sample.plant_battery_soc_percent,
        "available_discharge_kwh": sample.plant_battery_available_discharge_kwh,
    }


def load_device_metadata(database: SolarDatabase) -> DeviceInfo:
    """Load latest ``sigenstor_devices`` row for every metadata variable."""
    has_variable_name = any(
        column["name"] == "variable_name"
        for column in inspect(database.engine).get_columns(SigenStorDevice.__tablename__)
    )
    columns = [
        SigenStorDevice.id,
        SigenStorDevice.variable,
        SigenStorDevice.value,
        SigenStorDevice.unit,
    ]
    if has_variable_name:
        columns.append(SigenStorDevice.variable_name)
    with database.sessions() as session:
        rows = session.execute(
            select(*columns).order_by(SigenStorDevice.id.desc())
        ).mappings().all()
    latest: dict[str, float | str] = {}
    for row in rows:
        variable_name = row.get("variable_name") or re.sub(
            r"[^a-z0-9]+", "_", row["variable"].casefold()
        ).strip("_")
        suffix = {"kW": "kw", "kWh": "kwh", "kVA": "kva", "V": "v", "Hz": "hz", "%": "percent"}.get(row["unit"])
        if suffix and not variable_name.endswith(f"_{suffix}"):
            variable_name = f"{variable_name}_{suffix}"
        try:
            value: float | str = float(row["value"])
        except ValueError:
            value = row["value"]
        latest.setdefault(variable_name, value)
    missing = [name for name in DeviceInfo.__dataclass_fields__ if name not in latest]
    if missing:
        raise ValueError(f"collected device information is missing: {', '.join(missing)}")
    return DeviceInfo(**{name: latest[name] for name in DeviceInfo.__dataclass_fields__})


if __name__ == "__main__":
    main()
