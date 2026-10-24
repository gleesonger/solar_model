from __future__ import annotations
import logging
import math
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import inspect, select
from solar_model.config import Config
from solar_model.time_utils import add_elapsed, utc_instant
from solar_model.storage.schema import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample
from solar_model.storage.connection import SolarDatabase
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval


LOGGER = logging.getLogger(__name__)


def interval_energies(rows: list[ForecastSolarSample], panel_ids: tuple[int, ...], timezone: ZoneInfo, source: str) -> dict[datetime, float] | None:
    """Convert cumulative Forecast.Solar Wh values into interval kWh totals."""
    by_panel: dict[int, list[tuple[datetime, float]]] = {}
    for panel_id in panel_ids:
        field = f"panel_{panel_id}_{source}_watt_hours"
        points = [(utc_instant(datetime.fromisoformat(row.forecast_time)), getattr(row, field)) for row in rows if getattr(row, field) is not None]
        if not points:
            return None
        by_panel[panel_id] = sorted(points, key=lambda point: point[0])
    totals: dict[datetime, float] = defaultdict(float)
    for points in by_panel.values():
        previous_by_day: dict[datetime.date, float] = {}
        for timestamp, watt_hours in points:
            day = timestamp.astimezone(timezone).date()
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
        interval_start = add_elapsed(ends_at, -timedelta(microseconds=1))
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
        if ends_at < utc_instant(as_of):
            continue
        starts_at = previous_ends_at or add_elapsed(ends_at, -timedelta(seconds=config.forecast.interval_seconds))
        solar.append(ForecastInterval(starts_at, ends_at, float(solar_kwh)))
        previous_ends_at = ends_at

    projection_end = add_elapsed(as_of, timedelta(hours=config.energy_plan.num_projection_hours))
    if not solar:
        raise ValueError("the as-of forecast contains no future solar intervals")
    if utc_instant(solar[0].starts_at) > utc_instant(as_of):
        solar.insert(0, ForecastInterval(as_of, solar[0].starts_at, 0.0))
    if utc_instant(solar[-1].ends_at) < utc_instant(projection_end):
        solar.append(ForecastInterval(solar[-1].ends_at, projection_end, 0.0))

    load: list[ForecastInterval] = []
    starts_at = as_of.astimezone(timezone)
    while utc_instant(starts_at) < utc_instant(projection_end):
        expected_load_kwh = load_profile.get((starts_at.weekday(), starts_at.hour))
        if expected_load_kwh is None:
            raise ValueError(f"no historical load profile exists for {starts_at.isoformat()}")
        load.append(ForecastInterval(starts_at, add_elapsed(starts_at, timedelta(hours=1)), float(expected_load_kwh)))
        starts_at = add_elapsed(starts_at, timedelta(hours=1))

    return ForecastData(name="base", solar=solar, load=load)

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
