from __future__ import annotations
import logging
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from sqlalchemy import inspect, select
from solar_model.config import Config
from solar_model.time_utils import add_elapsed, utc_instant
from solar_model.storage.schema import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample
from solar_model.storage.connection import SolarDatabase
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval
from solar_model.planning.forecasts.house_load import predict_day
from solar_model.storage.queries.forecasting import load_hourly_house_load


LOGGER = logging.getLogger(__name__)


def _forecast_bounds(as_of: datetime, config: Config) -> tuple[datetime, datetime]:
    """Whole UTC hours around the projection, for paired scenario half-hours."""
    start = pd.Timestamp(utc_instant(as_of))
    end = start + pd.Timedelta(hours=config.energy_plan.num_projection_hours)
    return start.floor("h").to_pydatetime(), end.ceil("h").to_pydatetime()


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

def load_house_load_intervals(database: SolarDatabase, as_of: datetime, config: Config) -> list[ForecastInterval]:
    """Feed the production house forecaster into planning without future observations."""
    zone = ZoneInfo(config.timezone)
    settings = config.forecast.load.model_calibration_parameters
    cutoff = pd.Timestamp(datetime.combine(as_of.astimezone(zone).date(), time.min, zone))
    history = load_hourly_house_load(database.engine.url.database, zone,
        start=cutoff - pd.Timedelta(days=settings.lookback), end=cutoff)
    start, end = map(pd.Timestamp, _forecast_bounds(as_of, config))
    predictions = {}
    intervals = []
    for hour in pd.date_range(start.floor("h"), end, freq="h", inclusive="left"):
        local = hour.tz_convert(zone)
        day = local.date()
        if day not in predictions:
            predictions[day] = predict_day(history, day, zone, settings).set_index("hour").prediction_kwh
        energy = predictions[day].loc[local.hour]
        if pd.isna(energy):
            raise ValueError(f"no historical house-load profile exists for {local.isoformat()}")
        left, right = max(start, hour), min(end, hour + pd.Timedelta(hours=1))
        intervals.append(ForecastInterval(left.to_pydatetime(), right.to_pydatetime(),
            float(energy) * (right - left).total_seconds() / 3600))
    return intervals


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
    panel_ids = tuple(array.panel_id for array in config.forecast.solar.arrays)
    solar_intervals = interval_energies(rows, panel_ids, timezone, "adj") or interval_energies(rows, panel_ids, timezone, "raw")
    if not solar_intervals:
        raise ValueError("the as-of forecast contains no usable solar values")

    solar: list[ForecastInterval] = []
    coverage_start, projection_end = _forecast_bounds(as_of, config)
    previous_ends_at: datetime | None = None
    for ends_at, solar_kwh in sorted(solar_intervals.items()):
        if ends_at <= coverage_start:
            continue
        starts_at = previous_ends_at or add_elapsed(ends_at, -timedelta(seconds=config.forecast.solar.interval_seconds))
        left, right = max(starts_at, coverage_start), min(ends_at, projection_end)
        if left >= right:
            break
        solar.append(ForecastInterval(left, right, float(solar_kwh) * ((right - left) / (ends_at - starts_at))))
        previous_ends_at = ends_at
        if ends_at >= projection_end:
            break

    if not solar:
        raise ValueError("the as-of forecast contains no future solar intervals")
    if utc_instant(solar[0].starts_at) > coverage_start:
        solar.insert(0, ForecastInterval(coverage_start, solar[0].starts_at, 0.0))
    if utc_instant(solar[-1].ends_at) < utc_instant(projection_end):
        solar.append(ForecastInterval(solar[-1].ends_at, projection_end, 0.0))

    load = load_house_load_intervals(database, as_of, config)

    return ForecastData(solar=solar, load=load)


def load_plan_actuals(database: SolarDatabase, starts_at: datetime, ends_at: datetime, capacity_kwh: float) -> pd.DataFrame:
    """Measured power and SOC-based stored energy on a UTC minute grid, retaining gaps."""
    sample = SigenStorModbusSample
    with database.sessions() as session:
        rows = session.execute(select(
            sample.collected_at_utc, sample.plant_pv_power_kw, sample.plant_load_power_kw,
            sample.plant_battery_soc_percent, sample.plant_grid_power_kw,
        ).where(sample.collected_at_utc >= starts_at, sample.collected_at_utc <= ends_at)
            .order_by(sample.collected_at_utc)).all()
    frame = pd.DataFrame(rows, columns=["timestamp", "solar", "house_load", "soc", "grid"])
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True)
    frame["battery"] = pd.to_numeric(frame.pop("soc")) * capacity_kwh / 100
    grid = pd.to_numeric(frame.pop("grid"))
    frame["grid_import"] = grid.clip(lower=0)
    frame["grid_export"] = (-grid).clip(lower=0)
    if frame.empty:
        return frame
    return frame.set_index("timestamp").astype(float).resample("1min").mean().reset_index()

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
