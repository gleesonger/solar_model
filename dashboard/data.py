"""Data access and calculations shared by dashboard tabs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from math import ceil, floor
from time import perf_counter
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import Session

from common import LOGGER, heavy_work
from config import SolarArrayConfig
from database import (
    HOURLY_VALID_COUNT_COLUMNS,
    ForecastSolarSample,
    SigenStorDevice,
    SigenStorHourlySample,
    SigenStorModbusSample,
    create_engine_for_database,
)
from .models import (
    AnalysisRangeData,
    HOURLY_PERIOD_DAYS,
    HOURLY_PERIODS,
    HourlyRangeData,
    SUMMARY_LATEST_KEYS,
    TelemetryData,
)
from .chart_data_sql import ChartRequest

ForecastWattHoursReader = Callable[[ForecastSolarSample], float | None]

PV_STRING_POWER_COLUMNS = {
    "pv1": "inverter_pv1_power_kw",
    "pv2": "inverter_pv2_power_kw",
    "pv3": "inverter_pv3_power_kw",
    "pv4": "inverter_pv4_power_kw",
}
PV_STRING_ENERGY_COLUMNS = {
    "pv1": "inverter_pv1_energy_kwh_period",
    "pv2": "inverter_pv2_energy_kwh_period",
    "pv3": "inverter_pv3_energy_kwh_period",
    "pv4": "inverter_pv4_energy_kwh_period",
}

FORECAST_WATT_HOURS_READERS: dict[str, dict[int, ForecastWattHoursReader]] = {
    "adjusted": {
        1: lambda row: row.panel_1_adj_watt_hours,
        2: lambda row: row.panel_2_adj_watt_hours,
        3: lambda row: row.panel_3_adj_watt_hours,
        4: lambda row: row.panel_4_adj_watt_hours,
    },
    "raw": {
        1: lambda row: row.panel_1_raw_watt_hours,
        2: lambda row: row.panel_2_raw_watt_hours,
        3: lambda row: row.panel_3_raw_watt_hours,
        4: lambda row: row.panel_4_raw_watt_hours,
    },
}
IMPORT_COST_TARIFF_PREFIX = "import_cost_tariff__"
CUSTOM_CHART_ENERGY_METRICS = (
    "solar", "forecast_total", "forecast_raw_total", "load", "battery", "grid_import",
    "grid_export", "grid_import_cost", "grid_export_revenue", "net_cost",
    "no_solar_battery_import_cost", "import_rate", "export_rate",
)
ENERGY_SUMMARY_COLUMNS = (
    "solar_actual",
    "solar_forecast",
    "load",
    "grid_import",
    "grid_export",
    "grid_import_cost",
    "grid_export_revenue",
    "net_cost",
    "no_solar_battery_import_cost",
)

CURRENCY_SUMMARY_COLUMNS = frozenset({
    "grid_import_cost",
    "grid_export_revenue",
    "net_cost",
    "no_solar_battery_import_cost",
})


class _CustomChartTimings:
    """Accumulate repeated daily steps for one custom-chart request."""

    def __init__(self) -> None:
        self.elapsed: dict[str, float] = {}
        self.rows: dict[str, int] = {}

    def record(self, name: str, started_at: float) -> None:
        self.elapsed[name] = self.elapsed.get(name, 0.0) + perf_counter() - started_at

    def log(self) -> None:
        for name, elapsed in self.elapsed.items():
            row_count = f", {self.rows[name]} rows" if name in self.rows else ""
            LOGGER.info("Chart data step: %s (%.3fs%s)", name, elapsed, row_count)


def _record_chart_step(timings: _CustomChartTimings | None, name: str, started_at: float) -> None:
    if timings is not None:
        timings.record(name, started_at)


def _load_chart_rows(
    session: Session, statement, timings: _CustomChartTimings | None, name: str,
) -> list[Any]:
    started_at = perf_counter()
    result = session.scalars(statement)
    _record_chart_step(timings, f"{name}: execute SELECT and prepare ORM result", started_at)
    started_at = perf_counter()
    rows = list(result.all())
    fetch_step = f"{name}: fetch rows and construct ORM objects"
    _record_chart_step(timings, fetch_step, started_at)
    if timings is not None:
        timings.rows[fetch_step] = len(rows)
    return rows


def load_minute_samples(
    session: Session, statement, purpose: str, *, timings: _CustomChartTimings | None = None,
) -> list[SigenStorModbusSample]:
    """Execute a raw-minute query and log its returned-row count."""
    samples = _load_chart_rows(session, statement, timings, "Minute data")
    LOGGER.info("Minute-table query returned %s rows: %s", len(samples), purpose)
    return samples

@heavy_work("dashboard current-day summary query")
def load_day(
    database_path: str,
    timezone_name: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> pd.DataFrame:
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            # This is the immutable, official snapshot selected at the start
            # of the day.  It supplies both the raw and BoD-adjusted views.
            bod_forecast_rows = load_forecast_for_day(session, day_start)
            panel_ids = tuple(array.panel_id for array in forecast_arrays)
            bod_adjusted_forecast_rows = load_bod_adjusted_forecast_for_day(session, day_start, panel_ids)
            # This may be a later provider collection, with the intraday
            # adjustment recalculated at collection time.
            latest_adjusted_forecast_rows = load_latest_adjusted_forecast_for_day(session, day_start, panel_ids)
            # Preserve unknown future hours as NaN until the live forecast is
            # assembled below.  Filling them with zero here would make them
            # look like observed zero-PV hours.
            actual = load_actual_hourly(session, day_start, fill_missing=False)
            actual_by_array = load_actual_arrays_hourly(
                session, day_start, actuals_to_forecast, fill_missing=False,
            )
    finally:
        engine.dispose()

    latest_adjusted_forecast = aggregate_forecast(
        latest_adjusted_forecast_rows, day_start, forecast_arrays, source="adjusted",
    )
    bod_adjusted_forecast = aggregate_forecast(
        bod_adjusted_forecast_rows, day_start, forecast_arrays, source="adjusted",
    ).rename(columns={"forecast_total": "forecast_bod_total"})[["hour", "forecast_bod_total"]]
    raw_forecast = aggregate_forecast(
        bod_forecast_rows, day_start, forecast_arrays, source="raw",
    ).rename(columns={"forecast_total": "forecast_raw_total"})[["hour", "forecast_raw_total"]]
    hourly = (
        actual.merge(actual_by_array, on="hour", how="left")
        .merge(latest_adjusted_forecast, on="hour", how="left")
        .merge(bod_adjusted_forecast, on="hour", how="left")
        .merge(raw_forecast, on="hour", how="left")
    )
    # The live forecast is a daily energy estimate.  Completed local hours
    # always use actuals (including a zero substituted later for an absent
    # reading); otherwise a missing old actual would incorrectly retain an
    # old forecast into the evening.  For the still-open hour we use its
    # partial actual if present, and use forecast only when it is absent.
    hour_number = pd.to_numeric(hourly["hour"].str.slice(0, 2), errors="coerce")
    completed = hour_number.lt(now.hour)
    current_with_actual = hour_number.eq(now.hour) & hourly["solar"].notna()
    actual_mask = completed | current_with_actual
    hourly["latest_forecast_total"] = hourly["forecast_total"].where(
        ~actual_mask, hourly["solar"],
    )
    hourly = allocate_solar_energy_to_arrays(hourly, actuals_to_forecast).fillna(0.0)
    return hourly


def load_grid_energy_day_start(
    database_path: str,
    local_date: str,
    timezone_name: str,
) -> dict[str, float]:
    """Return the day's opening lifetime grid meters for live daily totals."""
    timezone = ZoneInfo(timezone_name)
    day_start = datetime.combine(date.fromisoformat(local_date), time.min, tzinfo=timezone)
    day_end = day_start + timedelta(days=1)
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            row = session.execute(
                select(
                    SigenStorModbusSample.plant_grid_import_total_kwh,
                    SigenStorModbusSample.plant_grid_export_total_kwh,
                )
                .where(SigenStorModbusSample.collected_at_utc >= day_start.astimezone(ZoneInfo("UTC")).isoformat())
                .where(SigenStorModbusSample.collected_at_utc < day_end.astimezone(ZoneInfo("UTC")).isoformat())
                .order_by(SigenStorModbusSample.collected_at_utc)
                .limit(1)
            ).first()
    finally:
        engine.dispose()

    if row is None:
        return {}
    return {
        "today_grid_import": float(row.plant_grid_import_total_kwh),
        "today_grid_export": float(row.plant_grid_export_total_kwh),
    }


def load_pv_array_energy_day_totals(
    database_path: str,
    local_date: str,
    timezone_name: str,
) -> dict[int, float]:
    """Return measured per-string PV energy for one local day."""
    timezone = ZoneInfo(timezone_name)
    day_start = datetime.combine(date.fromisoformat(local_date), time.min, tzinfo=timezone)
    day_end = day_start + timedelta(days=1)
    energy_columns = [
        getattr(SigenStorModbusSample, f"inverter_pv{panel_id}_energy_kwh_period")
        for panel_id in range(1, 5)
    ]
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            totals = session.execute(
                select(*(func.coalesce(func.sum(column), 0.0) for column in energy_columns))
                .where(SigenStorModbusSample.collected_at_utc >= day_start.astimezone(ZoneInfo("UTC")).isoformat())
                .where(SigenStorModbusSample.collected_at_utc < day_end.astimezone(ZoneInfo("UTC")).isoformat())
            ).one()
    finally:
        engine.dispose()
    return {panel_id: float(total) for panel_id, total in enumerate(totals, start=1)}


@heavy_work("dashboard recent daily energy summary")
def load_recent_daily_energy(
    database_path: str,
    timezone_name: str,
    end_date: date,
    days: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    daily, _ = _load_recent_energy_summaries(
        database_path, timezone_name, end_date, forecast_arrays, days=days,
    )
    return daily


def load_lifetime_start_date(database_path: str, fallback: date, timezone_name: str = "Europe/Dublin") -> date:
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            actual_start = session.scalar(
                select(func.min(SigenStorModbusSample.collected_at_utc))
            )
            forecast_start = session.scalar(
                select(func.min(ForecastSolarSample.collected_at_utc))
            )
    finally:
        engine.dispose()

    starts = [
        parse_time(str(value), ZoneInfo(timezone_name)).date()
        for value in (actual_start, forecast_start)
        if value is not None
    ]
    return min(starts, default=fallback)


@heavy_work("dashboard recent monthly and lifetime energy summary")
def load_recent_monthly_energy(
    database_path: str,
    timezone_name: str,
    end_date: date,
    months: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    _, monthly = _load_recent_energy_summaries(
        database_path, timezone_name, end_date, forecast_arrays, months=months,
    )
    return monthly


@heavy_work("dashboard recent daily monthly and lifetime energy summaries")
def load_recent_energy_summaries(
    database_path: str,
    timezone_name: str,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    days: int = 7,
    months: int = 12,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load both Recent tables from one shared native SQL aggregation."""
    return _load_recent_energy_summaries(
        database_path, timezone_name, end_date, forecast_arrays, days=days, months=months,
    )


def _load_recent_energy_summaries(
    database_path: str,
    timezone_name: str,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    *,
    days: int | None = None,
    months: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    from .summary_data_sql import load_energy_summaries

    if days is not None and days < 1:
        raise ValueError("Recent daily period must contain at least one day")
    if months is not None and months < 1:
        raise ValueError("Recent monthly period must contain at least one month")
    daily_start = end_date - timedelta(days=days - 1) if days is not None else None
    monthly_start = shift_date_by_months(end_date.replace(day=1), -(months - 1)) if months is not None else None
    lifetime_start = load_lifetime_start_date(database_path, end_date, timezone_name) if months is not None else None
    start = min(value for value in (daily_start, monthly_start, lifetime_start) if value is not None)
    frame = load_energy_summaries(
        database_path, timezone_name, start, end_date, forecast_arrays,
        datetime.now(ZoneInfo(timezone_name)), daily_start=daily_start,
        monthly_start=monthly_start, lifetime_start=lifetime_start,
    )
    daily = frame.loc[frame["kind"] == "daily"].copy()
    monthly = frame.loc[frame["kind"] == "monthly"].copy()
    daily["period"] = daily["label"].fillna(daily["date"])
    monthly["period"] = [
        label if pd.notna(label) else datetime.strptime(month, "%Y-%m").strftime("%b %Y")
        for month, label in zip(monthly["date"], monthly["label"])
    ]
    return (
        daily[["date", *ENERGY_SUMMARY_COLUMNS, "period"]].reset_index(drop=True),
        monthly[["date", "period", *ENERGY_SUMMARY_COLUMNS]].reset_index(drop=True),
    )


@heavy_work("dashboard daily energy aggregation")
def load_daily_energy_totals(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    from .summary_data_sql import load_energy_summaries

    frame = load_energy_summaries(
        database_path, timezone_name, start_date, end_date, forecast_arrays,
        datetime.now(ZoneInfo(timezone_name)), daily_start=start_date, include_totals=False,
    )
    return frame[["date", *ENERGY_SUMMARY_COLUMNS]].sort_values("date").reset_index(drop=True)


def append_energy_total(
    data: pd.DataFrame,
    label: str,
    *,
    source: pd.DataFrame | None = None,
) -> pd.DataFrame:
    values = source if source is not None else data
    total = {
        "period": label,
        **{
            column: dataframe_column(values, column).sum(min_count=1)
            for column in ENERGY_SUMMARY_COLUMNS
        },
    }
    return pd.concat([data, pd.DataFrame([total])], ignore_index=True)


def human_readable_age(age_seconds: float) -> str:
    seconds = max(0, int(age_seconds))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"
    if seconds < 3_600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86_400:
        hours = seconds // 3_600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86_400
    return f"{days} day{'s' if days != 1 else ''} ago"


def empty_hourly_range(
    power_interval_minutes: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> HourlyRangeData:
    hours = [f"{hour:02d}:00" for hour in range(24)]
    actual_columns = {
        actual_column_name(panel_id)
        for panel_id in actuals_to_forecast.values()
    }
    forecast_columns = {
        forecast_column_name(array.panel_id)
        for array in forecast_arrays
    }

    def empty_frame(
        axis: str,
        labels: list[str],
        columns: set[str],
    ) -> pd.DataFrame:
        return pd.DataFrame({
            axis: labels,
            **{column: [None] * len(labels) for column in sorted(columns)},
        })

    energy = empty_frame(
        "hour",
        hours,
        {
            "solar",
            "forecast_total",
            "load",
            "grid_import",
            "grid_export",
            *actual_columns,
            *forecast_columns,
        },
    )
    power = empty_frame(
        "time",
        [
            minute_of_day_label(minute)
            for minute in range(0, 24 * 60, power_interval_minutes)
        ],
        {
            "solar",
            "load",
            "battery",
            "inverter",
            "grid_import",
            "grid_export",
            *actual_columns,
        },
    )
    battery = empty_frame(
        "time",
        [
            minute_of_day_label(minute)
            for minute in range(0, 24 * 60, power_interval_minutes)
        ],
        {"available_energy_kwh", "soc_percent"},
    )
    return HourlyRangeData(energy=energy, power=power, battery=battery)


@heavy_work("dashboard hourly tab query and aggregation")
def load_hourly_range(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    power_interval_minutes: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> HourlyRangeData:
    if end_date < start_date:
        raise ValueError("End date must not be before first date")
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")

    timezone = ZoneInfo(timezone_name)
    daily_energy: list[pd.DataFrame] = []
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            selected_samples = load_minute_samples(session,
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    >= start_date.isoformat()
                )
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    <= end_date.isoformat()
                )
                .order_by(SigenStorModbusSample.collected_at_utc),
                f"Hourly tab {start_date.isoformat()} to {end_date.isoformat()}",
            )
            daily_energy = load_daily_energy_frames(
                session,
                timezone,
                start_date,
                end_date,
                forecast_arrays,
                actuals_to_forecast,
            )
    finally:
        engine.dispose()

    energy = average_daily_hourly_energy(daily_energy)
    power, battery = average_telemetry_by_interval(
        selected_samples,
        timezone,
        power_interval_minutes,
        actuals_to_forecast,
    )
    return HourlyRangeData(energy=energy, power=power, battery=battery)


@dataclass(frozen=True)
class AnalysisRangeBounds:
    start_date: date
    end_date: date
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class AnalysisSourceData:
    samples: list[SigenStorModbusSample]
    forecast_rows: list[ForecastSolarSample]


def analysis_range_bounds(
    end_date: date,
    count: int,
    unit: str,
    timezone: tzinfo,
) -> AnalysisRangeBounds:
    """Return the single, inclusive time range used by every Analysis chart."""
    calendar_start = historical_start_date(end_date, count, unit)
    end_at = datetime.combine(end_date, time.max, tzinfo=timezone)
    if unit == "hours":
        bucket_end = end_at.replace(minute=0, second=0, microsecond=0)
        start_at = bucket_end - timedelta(hours=count - 1)
    elif unit == "minutes":
        bucket_end = end_at.replace(second=0, microsecond=0)
        start_at = bucket_end - timedelta(minutes=count - 1)
    else:
        start_at = datetime.combine(calendar_start, time.min, tzinfo=timezone)
    return AnalysisRangeBounds(start_at.date(), end_date, start_at, end_at)


def analysis_date_bounds(
    start_date: date,
    end_date: date,
    timezone: tzinfo,
) -> AnalysisRangeBounds:
    if end_date < start_date:
        raise ValueError("End date must not be before start date")
    return AnalysisRangeBounds(
        start_date=start_date,
        end_date=end_date,
        start_at=datetime.combine(start_date, time.min, tzinfo=timezone),
        end_at=datetime.combine(end_date, time.max, tzinfo=timezone),
    )


@heavy_work("dashboard analysis source-data query")
def load_analysis_source_data(
    database_path: str,
    bounds: AnalysisRangeBounds,
    *,
    include_minute_samples: bool = True,
    timings: _CustomChartTimings | None = None,
) -> AnalysisSourceData:
    """Load forecasts and, when needed, raw records for Analysis charts."""
    started_at = perf_counter()
    engine = create_engine_for_database(database_path)
    _record_chart_step(timings, "Source data: engine setup", started_at)
    try:
        with Session(engine) as session:
            samples = (
                load_minute_samples(session,
                    select(SigenStorModbusSample)
                    .where(SigenStorModbusSample.collected_at_utc >= ceil(bounds.start_at.timestamp()))
                    .where(SigenStorModbusSample.collected_at_utc <= floor(bounds.end_at.timestamp()))
                    .order_by(SigenStorModbusSample.collected_at_utc),
                    f"Analysis {bounds.start_at.isoformat()} to {bounds.end_at.isoformat()}",
                    timings=timings,
                )
                if include_minute_samples
                else []
            )
            forecast_rows = _load_chart_rows(session,
                select(ForecastSolarSample)
                .where(ForecastSolarSample.collected_at_utc >= ceil(bounds.start_at.timestamp()))
                .where(ForecastSolarSample.collected_at_utc <= floor(bounds.end_at.timestamp()))
                .order_by(ForecastSolarSample.collected_at_utc, ForecastSolarSample.forecast_time),
                timings, "Forecast data",
            )
    finally:
        started_at = perf_counter()
        engine.dispose()
        _record_chart_step(timings, "Source data: engine disposal", started_at)
    return AnalysisSourceData(samples=samples, forecast_rows=forecast_rows)


def load_analysis_hourly_samples(
    database_path: str,
    bounds: AnalysisRangeBounds,
) -> list[SigenStorHourlySample]:
    """Load hourly rollup rows for an Analysis power/battery request."""
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            return list(session.scalars(
                select(SigenStorHourlySample)
                .where(SigenStorHourlySample.hour_start_utc >= bounds.start_at.isoformat())
                .where(SigenStorHourlySample.hour_start_utc <= bounds.end_at.isoformat())
                .order_by(SigenStorHourlySample.hour_start_utc)
            ).all())
    finally:
        engine.dispose()


SAMPLE_EXPORT_COLUMNS = tuple(
    column.name
    for column in SigenStorModbusSample.__table__.columns
    if column.name not in {"id", "collected_at_utc", "collected_at_local"}
)
FORECAST_EXPORT_COLUMNS = tuple(
    column.name
    for column in ForecastSolarSample.__table__.columns
    if column.name not in {"collected_at_utc", "collected_at_local", "forecast_time"}
)

# Retain the familiar wording for the fields already shown in Analysis.  All
# other database fields are included too, with an unambiguous readable label.
EXPORT_FIELD_LABELS = {
    "plant_pv_total_kwh_period": "Solar (kWh)",
    "plant_load_total_kwh_period": "Load (kWh)",
    "plant_battery_charge_total_kwh_period": "Battery Charge (kWh)",
    "plant_battery_discharge_total_kwh_period": "Battery Discharge (kWh)",
    "plant_grid_import_total_kwh_period": "Grid Imported (kWh)",
    "plant_grid_export_total_kwh_period": "Grid Exported (kWh)",
    "grid_import_cost_period": "Import Cost",
    "grid_export_revenue_period": "Export Revenue",
    "net_cost_period": "Net Cost",
    "no_solar_battery_import_cost_period": "Net Cost if No Solar",
    "import_rate": "Import Rate",
    "export_rate": "Export Rate",
}


def export_field_label(column: str, *, forecast: bool = False) -> str:
    if not forecast and column in EXPORT_FIELD_LABELS:
        return EXPORT_FIELD_LABELS[column]
    prefix = "Forecast " if forecast else ""
    return prefix + column.replace("_", " ").title()


def export_aggregation(column: str) -> str:
    """Return the appropriate interval aggregation for a database metric."""
    if column == "import_tariff_band":
        return "labels"
    if column.endswith("_kwh_period") or column.endswith("_cost_period") or column.endswith("_revenue_period"):
        return "sum"
    if column.endswith("_kwh") or column.endswith("_watt_hours_day"):
        # Cumulative and daily counters are point-in-time readings, so their
        # final reading represents the interval without double-counting.
        return "last"
    return "average"


def analysis_export_dataframe(
    database_path: str, bounds: AnalysisRangeBounds, timestep: str, timezone: tzinfo,
    *, include_forecast: bool,
) -> pd.DataFrame:
    """Aggregate export fields in SQL and fetch only the requested intervals."""
    from .export_data_sql import load_analysis_export_dataframe

    return load_analysis_export_dataframe(
        database_path, bounds, timestep, timezone, include_forecast=include_forecast,
    )


def analysis_interval_start(value: datetime, timestep: str) -> datetime:
    if timestep == "minute":
        return value.replace(second=0, microsecond=0)
    if timestep == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if timestep == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if timestep == "week":
        return datetime.combine(value.date() - timedelta(days=value.weekday()), time.min, tzinfo=value.tzinfo)
    if timestep in {"month", "season", "year"}:
        return datetime.combine(historical_bucket_start(value.date(), timestep), time.min, tzinfo=value.tzinfo)
    raise ValueError(f"Unsupported export timestep: {timestep}")


def aggregate_analysis_export_records(
    records: list[dict[str, object]], *, columns: tuple[str, ...], forecast: bool = False,
) -> pd.DataFrame:
    output_columns = ["period", *(export_field_label(column, forecast=forecast) for column in columns)]
    if not records:
        return pd.DataFrame(columns=output_columns)
    frame = pd.DataFrame(records)
    groups = frame.groupby("period", sort=True)
    result = pd.DataFrame(index=groups.size().index)
    for column in columns:
        aggregation = export_aggregation(column)
        label = export_field_label(column, forecast=forecast)
        if aggregation == "sum":
            result[label] = groups[column].sum(min_count=1)
        elif aggregation == "last":
            result[label] = groups[column].last()
        elif aggregation == "labels":
            # A bucket may cross tariff bands; retain each label once in reading order.
            result[label] = groups[column].agg(
                lambda values: " / ".join(dict.fromkeys(values.dropna())) or None
            )
        else:
            result[label] = groups[column].mean()
    return result.reset_index()


def analysis_forecast_export_records(
    forecast_rows: list[ForecastSolarSample], bounds: AnalysisRangeBounds, timestep: str,
    timezone: tzinfo,
) -> list[dict[str, object]]:
    first_guid_by_date: dict[str, str] = {}
    rows_by_guid: dict[str, list[ForecastSolarSample]] = {}
    for row in forecast_rows:
        if row.collected_at_utc is not None:
            first_guid_by_date.setdefault(parse_time(row.collected_at_utc, timezone).date().isoformat(), row.collected_at_utc)
            rows_by_guid.setdefault(row.collected_at_utc, []).append(row)
    records: list[dict[str, object]] = []
    for collection_timestamp in first_guid_by_date.values():
        # Preserve the chart behaviour of using the first forecast collection
        # captured on a date, then export every stored metric from that
        # collection at its forecast timestamp.
        for row in rows_by_guid.get(collection_timestamp, []):
            timestamp = parse_time(row.forecast_time, timezone)
            if not bounds.start_at <= timestamp <= bounds.end_at:
                continue
            records.append({
                "period": analysis_interval_start(timestamp, timestep),
                **{column: getattr(row, column) for column in FORECAST_EXPORT_COLUMNS},
            })
    return records


@heavy_work("dashboard analysis query and aggregation")
def load_analysis_range(
    database_path: str,
    timezone_name: str,
    bounds: AnalysisRangeBounds,
    frequency: str,
    power_interval_minutes: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    aggregation: str = "sum",
) -> AnalysisRangeData:
    """Request predefined, unsplit datasets from the shared chart SQL engine."""
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")

    started_at = perf_counter()
    timings = _CustomChartTimings()
    energy_selections = (
        *((metric, None) for metric in (
            "solar", "load", "grid_import", "grid_export", "grid_import_cost",
            "grid_export_revenue", "net_cost", "no_solar_battery_import_cost",
            IMPORT_COST_TARIFF_PREFIX + "*", "import_rate", "export_rate", "battery_flow",
        )),
        *((actual_column_name(panel), None) for panel in dict.fromkeys(actuals_to_forecast.values())),
        *((forecast_column_name(array.panel_id), None) for array in forecast_arrays),
        ("forecast_total", None), ("forecast_raw_total", None),
    )
    requires_minute_samples = bounds.start_date == bounds.end_date
    power_metrics = (
        "power_solar", *(f"power_actual_panel_{panel}" for panel in dict.fromkeys(actuals_to_forecast.values())),
        "power_load", "power_battery", "power_inverter", "power_grid_import", "power_grid_export",
    )
    datasets = load_chart_data_batch(
        database_path, timezone_name, bounds, forecast_arrays, actuals_to_forecast,
        (
            ChartRequest("energy", energy_selections, group_by=frequency, aggregation=aggregation),
            ChartRequest(
                "telemetry", tuple((metric, None) for metric in (*power_metrics, "battery_level", "soc_percent")),
                aggregation="average", sample_resolution="minute" if requires_minute_samples else "hour",
                interval_minutes=1 if requires_minute_samples else 60,
            ),
        ),
        timings=timings,
    )
    energy = chart_data_to_dataframe(datasets["energy"], column_names={"battery_flow": "battery"})
    telemetry_frame = chart_data_to_dataframe(datasets["telemetry"], period_column="time")
    power = telemetry_frame[["time", *power_metrics]].rename(columns={metric: metric.removeprefix("power_") for metric in power_metrics})
    battery = telemetry_frame[["time", "battery_level", "soc_percent"]].rename(columns={"battery_level": "available_energy_kwh"})
    timings.elapsed["Other request overhead"] = max(0.0, perf_counter() - started_at - sum(timings.elapsed.values()))
    timings.log()
    return AnalysisRangeData(energy=energy, power=power, battery=battery)


def load_rollup_daily_energy_frames(
    database_path: str,
    timezone: tzinfo,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    forecast_rows: list[ForecastSolarSample],
    *,
    timings: _CustomChartTimings | None = None,
) -> list[pd.DataFrame]:
    """Build Analysis energy frames from one hourly range query and forecasts."""
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone)
    started_at = perf_counter()
    engine = create_engine_for_database(database_path)
    _record_chart_step(timings, "Hourly data: engine setup", started_at)
    try:
        with Session(engine) as session:
            actual_samples = _load_chart_rows(session,
                select(SigenStorHourlySample)
                .where(SigenStorHourlySample.hour_start_utc >= start_at.isoformat())
                .where(SigenStorHourlySample.hour_start_utc < end_at.isoformat())
                .order_by(SigenStorHourlySample.hour_start_utc),
                timings, "Hourly data",
            )
    finally:
        started_at = perf_counter()
        engine.dispose()
        _record_chart_step(timings, "Hourly data: engine disposal", started_at)

    started_at = perf_counter()
    actual_by_date: dict[str, list[SigenStorHourlySample]] = {}
    for sample in actual_samples:
        date_key = parse_time(sample.hour_start_utc, timezone).date().isoformat()
        actual_by_date.setdefault(date_key, []).append(sample)
    _record_chart_step(timings, "Hourly data: index rows by local date", started_at)

    started_at = perf_counter()
    first_collection_by_date: dict[str, str] = {}
    forecasts_by_collection: dict[str, list[ForecastSolarSample]] = {}
    for row in forecast_rows:
        collection_date = parse_time(row.collected_at_utc, timezone).date().isoformat()
        first_collection_by_date.setdefault(collection_date, row.collected_at_utc)
        forecasts_by_collection.setdefault(row.collected_at_utc, []).append(row)
    _record_chart_step(timings, "Forecast data: index collections by local date", started_at)

    started_at = perf_counter()
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = [actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()]
    available_dates = sorted(
        date_key
        for date_key in (set(actual_by_date) | set(first_collection_by_date))
        if start_date.isoformat() <= date_key <= end_date.isoformat()
    )
    # Preserve the chart contract for a range with no actuals or forecasts,
    # without rebuilding every missing day in the requested range.
    if not available_dates:
        available_dates = [start_date.isoformat()]
    frames: list[pd.DataFrame] = []
    _record_chart_step(timings, "Daily frames: prepare date range", started_at)
    for date_key in available_dates:
        started_at = perf_counter()
        selected_date = date.fromisoformat(date_key)
        day_start = datetime(selected_date.year, selected_date.month, selected_date.day, tzinfo=timezone)
        day_samples = actual_by_date.get(date_key, [])
        if day_samples:
            actual = actual_hourly_from_rollup_samples(day_samples, day_start, fill_missing=False)
            _record_chart_step(timings, "Daily frames: actual totals", started_at)
            started_at = perf_counter()
            actual_by_array = actual_arrays_hourly_from_rollup_samples(
                day_samples, day_start, actuals_to_forecast, fill_missing=False,
            )
            _record_chart_step(timings, "Daily frames: actual panel values", started_at)
        else:
            actual = hours.assign(**{
                column: float("nan")
                for column in (
                    "solar", "load", "battery", "grid_import", "grid_export",
                    "grid_import_cost", "grid_export_revenue", "net_cost",
                    "no_solar_battery_import_cost", "import_rate", "export_rate",
                )
            })
            actual_by_array = hours.assign(**{column: float("nan") for column in actual_columns})
            _record_chart_step(timings, "Daily frames: empty-day frames", started_at)
        started_at = perf_counter()
        forecast = aggregate_forecast(
            forecasts_by_collection.get(first_collection_by_date.get(date_key, ""), []),
            day_start,
            forecast_arrays,
            fill_missing=False,
            source="adjusted",
        )
        _record_chart_step(timings, "Daily frames: adjusted forecast", started_at)
        started_at = perf_counter()
        raw_forecast = aggregate_forecast(
            forecasts_by_collection.get(first_collection_by_date.get(date_key, ""), []),
            day_start,
            forecast_arrays,
            fill_missing=False,
            source="raw",
        ).rename(columns={"forecast_total": "forecast_raw_total"})[["hour", "forecast_raw_total"]]
        _record_chart_step(timings, "Daily frames: raw forecast", started_at)
        started_at = perf_counter()
        frame = (
            actual.merge(actual_by_array, on="hour", how="left")
            .merge(forecast, on="hour", how="left")
            .merge(raw_forecast, on="hour", how="left")
        )
        _record_chart_step(timings, "Daily frames: merge actuals and forecasts", started_at)
        started_at = perf_counter()
        frame = allocate_solar_energy_to_arrays(frame, actuals_to_forecast)
        frame["date"] = date_key
        frames.append(frame)
        _record_chart_step(timings, "Daily frames: allocate panel energy and finalise", started_at)
    return frames


def analysis_daily_energy_frames(
    samples: list[SigenStorModbusSample],
    forecast_rows: list[ForecastSolarSample],
    timezone: tzinfo,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> list[pd.DataFrame]:
    """Build Analysis energy frames from the range's already-loaded records."""
    samples_by_date: dict[str, list[SigenStorModbusSample]] = {}
    for sample in samples:
        samples_by_date.setdefault(parse_time(sample.collected_at_utc, timezone).date().isoformat(), []).append(sample)

    first_guid_by_date: dict[str, str] = {}
    forecasts_by_guid: dict[str, list[ForecastSolarSample]] = {}
    for row in forecast_rows:
        if row.collected_at_utc is None:
            continue
        collection_date = parse_time(row.collected_at_utc, timezone).date().isoformat()
        first_guid_by_date.setdefault(collection_date, row.collected_at_utc)
        forecasts_by_guid.setdefault(row.collected_at_utc, []).append(row)

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = [actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()]
    frames: list[pd.DataFrame] = []
    for day_offset in range((end_date - start_date).days + 1):
        selected_date = start_date + timedelta(days=day_offset)
        date_text = selected_date.isoformat()
        day_start = datetime(selected_date.year, selected_date.month, selected_date.day, tzinfo=timezone)
        day_samples = samples_by_date.get(date_text, [])
        if day_samples:
            actual = actual_hourly_from_samples(day_samples, day_start, fill_missing=False)
            actual_by_array = actual_arrays_hourly_from_samples(
                day_samples, day_start, actuals_to_forecast, fill_missing=False
            )
        else:
            actual = hours.assign(
                solar=float("nan"), load=float("nan"), battery=float("nan"),
                grid_import=float("nan"), grid_export=float("nan"),
                grid_import_cost=float("nan"), grid_export_revenue=float("nan"),
                net_cost=float("nan"), no_solar_battery_import_cost=float("nan"),
                import_rate=float("nan"), export_rate=float("nan"),
            )
            actual_by_array = hours.assign(**{column: float("nan") for column in actual_columns})
        forecast = aggregate_forecast(
            forecasts_by_guid.get(first_guid_by_date.get(date_text, ""), []),
            day_start,
            forecast_arrays,
            fill_missing=False,
            source="adjusted",
        )
        raw_forecast = aggregate_forecast(
            forecasts_by_guid.get(first_guid_by_date.get(date_text, ""), []),
            day_start,
            forecast_arrays,
            fill_missing=False,
            source="raw",
        ).rename(columns={"forecast_total": "forecast_raw_total"})[["hour", "forecast_raw_total"]]
        frame = (
            actual.merge(actual_by_array, on="hour", how="left")
            .merge(forecast, on="hour", how="left")
            .merge(raw_forecast, on="hour", how="left")
        )
        frame = allocate_solar_energy_to_arrays(frame, actuals_to_forecast)
        frame["date"] = date_text
        frames.append(frame)
    return frames


@heavy_work("dashboard historical energy aggregation")
def load_historical_energy(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    frequency: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> pd.DataFrame:
    if end_date < start_date:
        raise ValueError("End date must not be before first date")
    if frequency not in {"minute", "hour", "day", "week", "month", "season", "year"}:
        raise ValueError(f"Unsupported historical frequency: {frequency}")

    timezone = ZoneInfo(timezone_name)
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            daily_frames = load_daily_energy_frames(
                session,
                timezone,
                start_date,
                end_date,
                forecast_arrays,
                actuals_to_forecast,
            )
    finally:
        engine.dispose()

    if frequency == "minute":
        return load_historical_minute_energy(
            database_path,
            timezone_name,
            start_date,
            end_date,
            forecast_arrays,
            actuals_to_forecast,
            start_at=start_at,
            end_at=end_at,
        )
    return historical_energy_from_frames(
        daily_frames,
        frequency,
        start_at or datetime.combine(start_date, time.min, tzinfo=timezone),
        end_at or datetime.combine(end_date, time.max, tzinfo=timezone),
    )


def historical_energy_from_frames(
    daily_frames: list[pd.DataFrame],
    frequency: str,
    start_at: datetime,
    end_at: datetime,
    *,
    aggregation: str = "sum",
) -> pd.DataFrame:
    if aggregation not in {"sum", "average"}:
        raise ValueError(f"Unsupported energy aggregation: {aggregation}")
    aggregation_method = "mean" if aggregation == "average" else "sum"
    combined = pd.concat(daily_frames, ignore_index=True)
    value_columns = [column for column in combined.columns if column not in {"date", "hour"}]
    rate_columns = [column for column in ("import_rate", "export_rate") if column in value_columns]
    sum_columns = [column for column in value_columns if column not in rate_columns]
    for column in value_columns:
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    timestamps = pd.to_datetime(
        dataframe_column(combined, "date") + " " + dataframe_column(combined, "hour")
    )
    combined = combined.loc[
        (timestamps >= start_at.replace(tzinfo=None))
        & (timestamps <= end_at.replace(tzinfo=None))
    ]
    if frequency == "hour":
        grouped = cast(pd.DataFrame, combined.groupby("hour", as_index=False).agg({
            **{column: aggregation_method for column in sum_columns},
            **{column: "mean" for column in rate_columns},
        }))
        grouped["period"] = dataframe_column(grouped, "hour")
        return cast(pd.DataFrame, grouped[["period", *value_columns]])
    combined["bucket_start"] = dataframe_column(combined, "date").map(
        lambda value: historical_bucket_start(date.fromisoformat(str(value)), frequency)
    )
    grouped = cast(pd.DataFrame, combined.groupby("bucket_start", as_index=False).agg({
        **{column: aggregation_method for column in sum_columns},
        **{column: "mean" for column in rate_columns},
    }))
    grouped["period"] = dataframe_column(grouped, "bucket_start").map(
        lambda value: historical_bucket_label(value, frequency)
    )
    return cast(pd.DataFrame, grouped[["period", *value_columns]])


@heavy_work("dashboard custom chart aggregation")
def load_custom_chart_preview(
    database_path: str,
    timezone_name: str,
    bounds: AnalysisRangeBounds,
    group_by: str,
    aggregation: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    selections: tuple[tuple[str, str | None], ...],
) -> dict[str, Any]:
    """Load the data required for the custom builder's energy-chart preview."""
    if not selections:
        return {"periods": [], "series": []}
    request_started_at = perf_counter()
    timings = _CustomChartTimings()
    preview = load_chart_data(
        database_path, timezone_name, bounds, group_by, aggregation,
        forecast_arrays, actuals_to_forecast, selections, timings=timings,
    )
    started_at = perf_counter()
    for series in preview["series"]:
        series["data"] = [chart_value(value) for value in series["data"]]
    timings.record("Chart: round preview values for display", started_at)
    elapsed = perf_counter() - request_started_at
    timings.elapsed["Other request overhead"] = max(0.0, elapsed - sum(timings.elapsed.values()))
    timings.log()
    return preview


def load_chart_data(
    database_path: str,
    timezone_name: str,
    bounds: AnalysisRangeBounds,
    group_by: str,
    aggregation: str,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    selections: tuple[tuple[str, str | None], ...],
    *,
    timings: _CustomChartTimings | None = None,
    sample_resolution: str = "hour",
    interval_minutes: int = 60,
) -> dict[str, Any]:
    """Aggregate selected variables once, at full precision, for either chart UI."""
    return load_chart_data_batch(
        database_path, timezone_name, bounds, forecast_arrays, actuals_to_forecast,
        (ChartRequest("chart", selections, group_by, aggregation, sample_resolution, interval_minutes),),
        timings=timings,
    )["chart"]


def load_chart_data_batch(
    database_path: str,
    timezone_name: str,
    bounds: AnalysisRangeBounds,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    requests: tuple[ChartRequest, ...],
    *,
    timings: _CustomChartTimings | None = None,
) -> dict[str, dict[str, Any]]:
    """Load every requested chart dataset with one shared SQL statement."""
    from .chart_data_sql import aggregate_chart_data_batch

    started_at = perf_counter()
    own_timings = timings is None
    timings = timings or _CustomChartTimings()
    result = aggregate_chart_data_batch(
        database_path, timezone_name, bounds, forecast_arrays, actuals_to_forecast, requests, timings,
    )
    if own_timings:
        timings.elapsed["Other request overhead"] = max(0.0, perf_counter() - started_at - sum(timings.elapsed.values()))
        timings.log()
    return result


def chart_data_to_dataframe(
    chart_data: dict[str, Any], *, period_column: str = "period", column_names: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Adapt unsplit shared results for Analysis tables without rounding values."""
    column_names = column_names or {}
    columns: dict[str, Any] = {period_column: chart_data["periods"]}
    for series in chart_data["series"]:
        if series["split"] is not None:
            raise ValueError("Analysis tables require unsplit chart data")
        name = column_names.get(series["metric"], series["metric"])
        columns[name] = pd.Series(series["data"], dtype="float64")
    return pd.DataFrame(columns)


def custom_chart_preview_from_frames(
    daily_frames: list[pd.DataFrame],
    start_at: datetime,
    end_at: datetime,
    group_by: str,
    aggregation: str,
    selections: tuple[tuple[str, str | None], ...],
    *,
    timings: _CustomChartTimings | None = None,
) -> dict[str, Any]:
    """Return ECharts-ready series for selected energy metrics and time splits."""
    if group_by not in {"hour", "day", "week", "month", "season", "year"}:
        raise ValueError(f"Unsupported custom chart grouping: {group_by}")
    if aggregation not in {"sum", "average"}:
        raise ValueError(f"Unsupported custom chart aggregation: {aggregation}")
    started_at = perf_counter()
    frame = pd.concat(daily_frames, ignore_index=True).copy()
    _record_chart_step(timings, "Chart: concatenate daily frames", started_at)
    started_at = perf_counter()
    timestamps = pd.to_datetime(dataframe_column(frame, "date") + " " + dataframe_column(frame, "hour"))
    frame = frame.loc[(timestamps >= start_at.replace(tzinfo=None)) & (timestamps <= end_at.replace(tzinfo=None))].copy()
    _record_chart_step(timings, "Chart: parse timestamps and filter range", started_at)
    if frame.empty:
        return {"periods": [], "series": [], "empty_message": "No data in the selected Analysis range"}
    started_at = perf_counter()
    if group_by == "hour":
        frame["_bucket"] = dataframe_column(frame, "hour")
        frame["_period"] = dataframe_column(frame, "hour")
    else:
        frame["_bucket"] = dataframe_column(frame, "date").map(
            lambda value: historical_bucket_start(date.fromisoformat(str(value)), group_by)
        )
        frame["_period"] = dataframe_column(frame, "_bucket").map(
            lambda value: historical_bucket_label(value, group_by)
        )
    periods = frame[["_bucket", "_period"]].drop_duplicates().sort_values("_bucket")
    _record_chart_step(timings, "Chart: calculate grouping buckets and periods", started_at)
    result_series: list[dict[str, Any]] = []
    for metric, split_by in selections:
        if metric not in frame.columns:
            continue
        started_at = perf_counter()
        values = pd.to_numeric(dataframe_column(frame, metric), errors="coerce")
        series_frame = frame[["_bucket", "_period", "date", "hour"]].copy()
        series_frame[metric] = values
        method = "mean" if aggregation == "average" or metric in {"battery", "import_rate", "export_rate"} else "sum"
        _record_chart_step(timings, f"Chart {metric}: prepare values", started_at)
        if split_by is None:
            started_at = perf_counter()
            grouped = series_frame.groupby(["_bucket", "_period"], as_index=False)[metric].agg(method)
            _record_chart_step(timings, f"Chart {metric}: aggregate values", started_at)
            started_at = perf_counter()
            values_by_bucket = grouped.set_index("_bucket")[metric]
            result_series.append({
                "metric": metric, "split": None,
                "data": [chart_value(values_by_bucket.get(bucket)) for bucket in periods["_bucket"]],
            })
            _record_chart_step(timings, f"Chart {metric}: format output series", started_at)
            continue
        if split_by == group_by:
            raise ValueError("Split by must be different from Group by")
        started_at = perf_counter()
        split_values = [
            custom_chart_split_bucket(date.fromisoformat(str(day)), str(hour), split_by)
            for day, hour in zip(dataframe_column(series_frame, "date"), dataframe_column(series_frame, "hour"), strict=True)
        ]
        series_frame[["_split_order", "_split"]] = pd.DataFrame(split_values, index=series_frame.index)
        _record_chart_step(timings, f"Chart {metric}: calculate split buckets", started_at)
        started_at = perf_counter()
        grouped = series_frame.groupby(["_bucket", "_split_order", "_split"], as_index=False)[metric].agg(method)
        _record_chart_step(timings, f"Chart {metric}: aggregate values", started_at)
        started_at = perf_counter()
        split_labels = grouped[["_split_order", "_split"]].drop_duplicates().sort_values("_split_order")["_split"]
        for split_label in split_labels:
            values_by_bucket = grouped.loc[grouped["_split"] == split_label].set_index("_bucket")[metric]
            result_series.append({
                "metric": metric, "split": str(split_label),
                "data": [chart_value(values_by_bucket.get(bucket)) for bucket in periods["_bucket"]],
            })
        _record_chart_step(timings, f"Chart {metric}: format output series", started_at)
    return {"periods": periods["_period"].tolist(), "series": result_series}


def chart_value(value: object) -> float | None:
    return None if pd.isna(value) else round(float(value), 1)


def custom_chart_split_bucket(value: date, hour: str, split_by: str) -> tuple[int, str]:
    if split_by == "hour":
        return int(hour[:2]), hour
    if split_by == "day":
        return value.weekday(), value.strftime("%a")
    if split_by == "week":
        week = value.isocalendar().week
        return week, f"Week {week:02d}"
    if split_by == "month":
        return value.month, value.strftime("%b")
    if split_by == "season":
        return ((value.month % 12) // 3), ("Winter", "Spring", "Summer", "Autumn")[(value.month % 12) // 3]
    if split_by == "year":
        return value.year, str(value.year)
    raise ValueError(f"Unsupported custom chart split: {split_by}")


@heavy_work("dashboard historical minute-energy aggregation")
def load_historical_minute_energy(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> pd.DataFrame:
    """Aggregate stored per-sample energy and tariff values into calendar minutes."""
    timezone = ZoneInfo(timezone_name)
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            samples = load_minute_samples(session,
                select(SigenStorModbusSample)
                .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) >= start_date.isoformat())
                .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) <= end_date.isoformat())
                .order_by(SigenStorModbusSample.collected_at_utc),
                f"Historical minute energy {start_date.isoformat()} to {end_date.isoformat()}",
            )
    finally:
        engine.dispose()

    array_columns = [
        *(actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()),
        *(forecast_column_name(array.panel_id) for array in forecast_arrays),
    ]
    value_columns = [
        "solar",
        "forecast_total",
        "forecast_raw_total",
        "load",
        "battery",
        "grid_import",
        "grid_export",
        "grid_import_cost",
        "grid_export_revenue",
        "net_cost",
        "no_solar_battery_import_cost",
        *array_columns,
    ]
    rows: list[dict[str, Any]] = []
    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone).replace(second=0, microsecond=0)
        if start_at is not None and timestamp < start_at.replace(second=0, microsecond=0):
            continue
        if end_at is not None and timestamp > end_at.replace(second=0, microsecond=0):
            continue
        rows.append({
            "period": timestamp.strftime("%Y-%m-%d %H:%M"),
            "solar": sample.plant_pv_total_kwh_period,
            "forecast_total": None,
            "forecast_raw_total": None,
            "load": sample.plant_load_total_kwh_period,
            "battery": (
                (sample.plant_battery_charge_total_kwh_period or 0.0)
                - (sample.plant_battery_discharge_total_kwh_period or 0.0)
            ),
            "grid_import": sample.plant_grid_import_total_kwh_period,
            "grid_export": sample.plant_grid_export_total_kwh_period,
            "grid_import_cost": sample.grid_import_cost_period,
            "grid_export_revenue": sample.grid_export_revenue_period,
            "net_cost": sample.net_cost_period,
            "no_solar_battery_import_cost": sample.no_solar_battery_import_cost_period,
            **{column: None for column in array_columns},
        })
    if not rows:
        return pd.DataFrame(columns=["period", *value_columns])
    frame = pd.DataFrame(rows)
    return cast(
        pd.DataFrame,
        frame.groupby("period", as_index=False)[value_columns].sum(min_count=1),
    )


@heavy_work("dashboard daily energy frame aggregation")
def load_daily_energy_frames(
    session: Session,
    timezone: tzinfo,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
) -> list[pd.DataFrame]:
    daily_energy: list[pd.DataFrame] = []
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    actual_dates = set(session.scalars(
        select(func.substr(SigenStorModbusSample.collected_at_local, 1, 10))
        .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) >= start_text)
        .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) <= end_text)
        .distinct()
    ).all())
    forecast_dates = set(session.scalars(
        select(func.substr(ForecastSolarSample.collected_at_local, 1, 10))
        .where(func.substr(ForecastSolarSample.collected_at_local, 1, 10) >= start_text)
        .where(func.substr(ForecastSolarSample.collected_at_local, 1, 10) <= end_text)
        .distinct()
    ).all())
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = [actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()]
    number_of_days = (end_date - start_date).days + 1
    for day_offset in range(number_of_days):
        selected_date = start_date + timedelta(days=day_offset)
        selected_date_text = selected_date.isoformat()
        day_start = datetime(
            selected_date.year,
            selected_date.month,
            selected_date.day,
            tzinfo=timezone,
        )
        if selected_date_text in actual_dates:
            actual = load_actual_hourly(session, day_start, fill_missing=False)
            actual_by_array = load_actual_arrays_hourly(
                session,
                day_start,
                actuals_to_forecast,
                fill_missing=False,
            )
        else:
            actual = hours.assign(
                solar=float("nan"),
                load=float("nan"),
                battery=float("nan"),
                grid_import=float("nan"),
                grid_export=float("nan"),
                grid_import_cost=float("nan"),
                grid_export_revenue=float("nan"),
                net_cost=float("nan"),
                no_solar_battery_import_cost=float("nan"),
            )
            actual_by_array = hours.assign(
                **{column: float("nan") for column in actual_columns}
            )
        raw_forecast_rows = (
            load_forecast_for_day(session, day_start)
            if selected_date_text in forecast_dates
            else []
        )
        adjusted_forecast_rows = (
            load_bod_adjusted_forecast_for_day(session, day_start)
            if selected_date_text in forecast_dates
            else []
        )
        forecast = aggregate_forecast(
            adjusted_forecast_rows, day_start, forecast_arrays, fill_missing=False, source="adjusted",
        )
        raw_forecast = aggregate_forecast(
            raw_forecast_rows, day_start, forecast_arrays, fill_missing=False, source="raw",
        ).rename(columns={"forecast_total": "forecast_raw_total"})[["hour", "forecast_raw_total"]]
        day = (
            actual.merge(actual_by_array, on="hour", how="left")
            .merge(forecast, on="hour", how="left")
            .merge(raw_forecast, on="hour", how="left")
        )
        day = allocate_solar_energy_to_arrays(day, actuals_to_forecast)
        day["date"] = selected_date.isoformat()
        daily_energy.append(day)
    return daily_energy


def historical_start_date(end_date: date, count: int, unit: str) -> date:
    if count < 1:
        raise ValueError("Historical period must be a positive whole number")
    if unit == "minutes":
        return end_date - timedelta(days=(count - 1) // (24 * 60))
    if unit == "hours":
        return end_date - timedelta(days=(count - 1) // 24)
    if unit == "days":
        return end_date - timedelta(days=count - 1)
    if unit == "weeks":
        return end_date - timedelta(weeks=count) + timedelta(days=1)
    if unit == "months":
        return shift_date_by_months(end_date, -count) + timedelta(days=1)
    if unit == "years":
        return shift_date_by_months(end_date, -12 * count) + timedelta(days=1)
    raise ValueError(f"Unsupported historical period: {unit}")


def hourly_period_name(start_date: date, end_date: date) -> str:
    days = (end_date - start_date).days + 1
    return next(
        (
            period
            for period, period_days in HOURLY_PERIOD_DAYS.items()
            if period_days == days
        ),
        "Custom",
    )


def next_hourly_period(period: str) -> str:
    if period not in HOURLY_PERIODS:
        return "Today"
    index = HOURLY_PERIODS.index(period)
    return HOURLY_PERIODS[(index + 1) % len(HOURLY_PERIODS)]


def hourly_period_range(
    period: str,
    end_date: date,
    lifetime_start: date,
) -> tuple[date, date]:
    if period == "Lifetime":
        return min(lifetime_start, end_date), end_date
    days = HOURLY_PERIOD_DAYS.get(period)
    if days is None:
        raise ValueError(f"Unsupported hourly period: {period}")
    return end_date - timedelta(days=days - 1), end_date


def shift_analysis_date(value: date, count: int, unit: str, direction: int) -> date:
    """Navigate the date anchor; hour ranges move one calendar day per click."""
    if unit == "hours":
        return value + timedelta(days=direction)
    if unit == "days":
        return value + timedelta(days=direction * count)
    if unit == "weeks":
        return value + timedelta(weeks=direction * count)
    if unit in {"months", "years"}:
        return shift_date_by_months(value, direction * count * (12 if unit == "years" else 1))
    raise ValueError("Select a valid period")


def shift_date_by_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    following_month = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (following_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def historical_bucket_start(value: date, frequency: str) -> date:
    if frequency == "day":
        return value
    if frequency == "week":
        return value - timedelta(days=value.weekday())
    if frequency == "month":
        return value.replace(day=1)
    if frequency == "season":
        if value.month in {12, 1, 2}:
            winter_year = value.year if value.month == 12 else value.year - 1
            return date(winter_year, 12, 1)
        season_month = 3 if value.month <= 5 else 6 if value.month <= 8 else 9
        return date(value.year, season_month, 1)
    if frequency == "year":
        return date(value.year, 1, 1)
    raise ValueError(f"Unsupported historical frequency: {frequency}")


def historical_bucket_label(value: date, frequency: str) -> str:
    if frequency == "day":
        return value.isoformat()
    if frequency == "week":
        return f"Week of {value:%d %b %Y}"
    if frequency == "month":
        return value.strftime("%b %Y")
    if frequency == "season":
        season = {12: "Winter", 3: "Spring", 6: "Summer", 9: "Autumn"}[value.month]
        if value.month == 12:
            return f"{season} {value.year}/{str(value.year + 1)[-2:]}"
        return f"{season} {value.year}"
    if frequency == "year":
        return str(value.year)
    raise ValueError(f"Unsupported historical frequency: {frequency}")


@heavy_work("dashboard telemetry query and aggregation")
def load_telemetry(
    database_path: str,
    timezone_name: str,
) -> TelemetryData:
    timezone = ZoneInfo(timezone_name)
    day_start = datetime.now(timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            samples = load_minute_samples(session,
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    == day_start.strftime("%Y-%m-%d")
                )
                .order_by(SigenStorModbusSample.collected_at_utc),
                "15-minute telemetry for today",
            )
            full_updated_last_local = session.scalar(
                select(SigenStorModbusSample.collected_at_local)
                .order_by(SigenStorModbusSample.collected_at_utc.desc())
                .limit(1)
            )
    finally:
        engine.dispose()

    power_totals: dict[tuple[str, str], float] = {}
    power_counts: dict[tuple[str, str], int] = {}
    battery_totals: dict[tuple[str, str], float] = {}
    battery_counts: dict[tuple[str, str], int] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone)
        power_bucket = quarter_hour_label(timestamp)
        grid_power = sample.plant_grid_power_kw
        power_values = {
            "solar": sample.plant_pv_power_kw,
            "load": sample.plant_load_power_kw,
            "battery": sample.plant_battery_power_kw,
            "inverter": sample.inverter_power_kw,
            "grid_import": max(grid_power, 0.0) if grid_power is not None else None,
            "grid_export": max(-grid_power, 0.0) if grid_power is not None else None,
        }
        for name, value in power_values.items():
            if value is not None:
                key = (power_bucket, name)
                power_totals[key] = power_totals.get(key, 0.0) + value
                power_counts[key] = power_counts.get(key, 0) + 1

        battery_values = {
            "available_energy_kwh": sample.plant_battery_available_discharge_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for name, value in battery_values.items():
            if value is not None:
                key = (power_bucket, name)
                battery_totals[key] = battery_totals.get(key, 0.0) + value
                battery_counts[key] = battery_counts.get(key, 0) + 1

    bucket_labels = [quarter_hour_label(day_start + timedelta(minutes=15 * index)) for index in range(96)]
    power_rows: list[dict[str, Any]] = []
    battery_rows: list[dict[str, Any]] = []
    for bucket in bucket_labels:
        power_row: dict[str, Any] = {"time": bucket}
        for name in ("solar", "load", "battery", "inverter", "grid_import", "grid_export"):
            key = (bucket, name)
            count = power_counts.get(key, 0)
            power_row[name] = power_totals.get(key, 0.0) / count if count else None
        power_rows.append(power_row)
        battery_row: dict[str, Any] = {"time": bucket}
        for name in ("available_energy_kwh", "soc_percent"):
            key = (bucket, name)
            count = battery_counts.get(key, 0)
            battery_row[name] = battery_totals.get(key, 0.0) / count if count else None
        battery_rows.append(battery_row)

    latest_sample = samples[-1] if samples else None
    latest_grid = latest_sample.plant_grid_power_kw if latest_sample is not None else None
    latest = {
        "solar": latest_sample.plant_pv_power_kw if latest_sample is not None else None,
        "battery": latest_sample.plant_battery_power_kw if latest_sample is not None else None,
        "inverter": latest_sample.inverter_power_kw if latest_sample is not None else None,
        "load": latest_sample.plant_load_power_kw if latest_sample is not None else None,
        "grid_import": max(latest_grid, 0.0) if latest_grid is not None else None,
        "grid_export": max(-latest_grid, 0.0) if latest_grid is not None else None,
        "battery_available_energy_kwh": (
            latest_sample.plant_battery_available_discharge_kwh
            if latest_sample is not None
            else None
        ),
        "battery_soc_percent": (
            latest_sample.plant_battery_soc_percent if latest_sample is not None else None
        ),
    }
    return TelemetryData(
        power_15m=pd.DataFrame(power_rows),
        battery_15m=pd.DataFrame(battery_rows),
        latest=latest,
        latest_collected_at_utc=(
            latest_sample.collected_at_utc if latest_sample is not None else None
        ),
        full_updated_last_local=full_updated_last_local,
    )


def load_latest_collection_local(database_path: str, timezone_name: str) -> str | None:
    """Return the most recent raw collection timestamp via the primary key."""
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            latest_utc = session.scalar(
                select(SigenStorModbusSample.collected_at_utc)
                .order_by(SigenStorModbusSample.collected_at_utc.desc())
                .limit(1)
            )
    finally:
        engine.dispose()
    if latest_utc is None:
        return None
    return datetime.fromisoformat(latest_utc).astimezone(ZoneInfo(timezone_name)).isoformat()


def average_daily_hourly_energy(daily_frames: list[pd.DataFrame]) -> pd.DataFrame:
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    if not daily_frames:
        return hours

    combined = pd.concat(daily_frames, ignore_index=True)
    value_columns = [column for column in combined.columns if column not in {"date", "hour"}]
    for column in value_columns:
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    averaged = cast(pd.DataFrame, combined.groupby("hour", as_index=False)[value_columns].mean())
    return hours.merge(averaged, on="hour", how="left")


def average_telemetry_by_interval(
    samples: list[SigenStorModbusSample],
    timezone: tzinfo,
    power_interval_minutes: int,
    actuals_to_forecast: dict[str, int],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")
    array_power_names = tuple(
        actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()
    )
    power_names = (
        "solar",
        *array_power_names,
        "load",
        "battery",
        "inverter",
        "grid_import",
        "grid_export",
    )
    battery_names = ("available_energy_kwh", "soc_percent")
    daily_totals: dict[tuple[str, str, str], float] = {}
    daily_counts: dict[tuple[str, str, str], int] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_local, timezone)
        day = timestamp.date().isoformat()
        power_bucket = interval_label(timestamp, power_interval_minutes)
        grid_power = sample.plant_grid_power_kw
        values: dict[str, float | None] = {
            "solar": sample.plant_pv_power_kw,
            "load": sample.plant_load_power_kw,
            "battery": sample.plant_battery_power_kw,
            "inverter": sample.inverter_power_kw,
            "grid_import": max(grid_power, 0.0) if grid_power is not None else None,
            "grid_export": max(-grid_power, 0.0) if grid_power is not None else None,
            "available_energy_kwh": sample.plant_battery_available_discharge_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for pv_string, panel_id in actuals_to_forecast.items():
            values[actual_column_name(panel_id)] = getattr(
                sample, PV_STRING_POWER_COLUMNS[pv_string]
            )
        for name, value in values.items():
            if value is None:
                continue
            key = (day, power_bucket, name)
            daily_totals[key] = daily_totals.get(key, 0.0) + value
            daily_counts[key] = daily_counts.get(key, 0) + 1

    hourly_totals: dict[tuple[str, str], float] = {}
    hourly_counts: dict[tuple[str, str], int] = {}
    for daily_key, total in daily_totals.items():
        _, hour, name = daily_key
        key = (hour, name)
        daily_average = total / daily_counts[daily_key]
        hourly_totals[key] = hourly_totals.get(key, 0.0) + daily_average
        hourly_counts[key] = hourly_counts.get(key, 0) + 1

    def build_frame(names: tuple[str, ...], bucket_labels: list[str]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for bucket in bucket_labels:
            row: dict[str, Any] = {"time": bucket}
            for name in names:
                key = (bucket, name)
                count = hourly_counts.get(key, 0)
                row[name] = hourly_totals.get(key, 0.0) / count if count else None
            rows.append(row)
        return pd.DataFrame(rows)

    power_buckets = [
        minute_of_day_label(minute)
        for minute in range(0, 24 * 60, power_interval_minutes)
    ]
    battery_buckets = power_buckets
    return build_frame(power_names, power_buckets), build_frame(battery_names, battery_buckets)


def average_hourly_rollup_telemetry(
    samples: list[SigenStorHourlySample],
    timezone: tzinfo,
    actuals_to_forecast: dict[str, int],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build Analysis hourly power/battery views from hourly sum/count rows."""
    power_names = (
        "solar",
        *(actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()),
        "load", "battery", "inverter", "grid_import", "grid_export",
    )
    battery_names = ("available_energy_kwh", "soc_percent")
    totals: dict[tuple[str, str, str], float] = {}
    counts: dict[tuple[str, str, str], int] = {}
    for sample in samples:
        timestamp = parse_time(sample.hour_start_utc, timezone)
        day = timestamp.date().isoformat()
        bucket = timestamp.strftime("%H:%M")
        value_columns = {
            "solar": "plant_pv_power_kw", "load": "plant_load_power_kw",
            "battery": "plant_battery_power_kw", "inverter": "inverter_power_kw",
            "available_energy_kwh": "plant_battery_available_discharge_kwh",
            "soc_percent": "plant_battery_soc_percent",
        }
        values: dict[str, float | None] = {
            "solar": sample.plant_pv_power_kw,
            "load": sample.plant_load_power_kw,
            "battery": sample.plant_battery_power_kw,
            "inverter": sample.inverter_power_kw,
            "grid_import": sample.plant_grid_import_power_kw,
            "grid_export": sample.plant_grid_export_power_kw,
            "available_energy_kwh": sample.plant_battery_available_discharge_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for pv_string, panel_id in actuals_to_forecast.items():
            values[actual_column_name(panel_id)] = getattr(
                sample, PV_STRING_POWER_COLUMNS[pv_string]
            )
            value_columns[actual_column_name(panel_id)] = PV_STRING_POWER_COLUMNS[pv_string]
        for name, value in values.items():
            if value is None:
                continue
            key = (day, bucket, name)
            totals[key] = totals.get(key, 0.0) + value
            count_column = "grid_power_sample_count" if name in {"grid_import", "grid_export"} else HOURLY_VALID_COUNT_COLUMNS[value_columns[name]]
            count = getattr(sample, count_column) or 0
            counts[key] = counts.get(key, 0) + count

    def frame(names: tuple[str, ...]) -> pd.DataFrame:
        rows: list[dict[str, float | str | None]] = []
        for hour in range(24):
            bucket = f"{hour:02d}:00"
            row: dict[str, float | str | None] = {"time": bucket}
            for name in names:
                daily_values = [
                    totals[key] / counts[key]
                    for key in totals
                    if key[1] == bucket and key[2] == name and counts[key]
                ]
                row[name] = sum(daily_values) / len(daily_values) if daily_values else None
            rows.append(row)
        return pd.DataFrame(rows)

    return frame(power_names), frame(battery_names)


@heavy_work("dashboard device-information query")
def load_device_information(database_path: str) -> list[dict[str, str]]:
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(SigenStorDevice).order_by(SigenStorDevice.id.desc())
            ).all()
    finally:
        engine.dispose()

    latest_by_variable: dict[str, SigenStorDevice] = {}
    for row in rows:
        latest_by_variable.setdefault(row.variable, row)
    return [
        {"variable": row.variable, "value": row.value, "unit": row.unit}
        for row in sorted(latest_by_variable.values(), key=lambda item: item.variable.casefold())
    ]


def load_forecast_for_day(session: Session, day_start: datetime) -> list[ForecastSolarSample]:
    # The operational model deliberately uses one provider snapshot for the
    # entire local day: the last one available before midnight.
    first_forecast = session.scalar(
        select(ForecastSolarSample.collected_at_utc)
        .where(ForecastSolarSample.collected_at_utc <= day_start.isoformat())
        .order_by(ForecastSolarSample.collected_at_utc.desc())
        .limit(1)
    )
    if not first_forecast:
        return []
    return list(session.scalars(
        select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == first_forecast)
    ).all())


def load_latest_adjusted_forecast_for_day(session: Session, day_start: datetime, panel_ids: tuple[int, ...] = (1, 2, 3, 4)) -> list[ForecastSolarSample]:
    """Return the newest collection snapshot holding adjusted day values."""
    next_day = day_start + timedelta(days=1)
    snapshot = session.scalar(
        select(ForecastSolarSample.collected_at_utc)
        .where(ForecastSolarSample.collected_at_utc >= day_start.isoformat())
        .where(ForecastSolarSample.collected_at_utc < next_day.isoformat())
        .where(ForecastSolarSample.forecast_time >= day_start.isoformat())
        .where(ForecastSolarSample.forecast_time < next_day.isoformat())
        .where(or_(*(getattr(ForecastSolarSample, f"panel_{panel}_adj_watts").is_not(None) for panel in panel_ids)) if panel_ids else False)
        .order_by(ForecastSolarSample.collected_at_utc.desc())
        .limit(1)
    )
    if snapshot is None:
        # Before the first same-day refresh, the official start-of-day row may
        # already contain the initial adjusted forecast.
        return load_forecast_for_day(session, day_start)
    return list(session.scalars(
        select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == snapshot)
    ).all())


def load_bod_adjusted_forecast_for_day(session: Session, day_start: datetime, panel_ids: tuple[int, ...] = (1, 2, 3, 4)) -> list[ForecastSolarSample]:
    """Return the first adjusted forecast collected during the local day."""
    next_day = day_start + timedelta(days=1)
    snapshot = session.scalar(
        select(ForecastSolarSample.collected_at_utc)
        .where(ForecastSolarSample.collected_at_utc >= day_start.isoformat())
        .where(ForecastSolarSample.collected_at_utc < next_day.isoformat())
        .where(ForecastSolarSample.forecast_time >= day_start.isoformat())
        .where(ForecastSolarSample.forecast_time < next_day.isoformat())
        .where(or_(*(getattr(ForecastSolarSample, f"panel_{panel}_adj_watt_hours").is_not(None) for panel in panel_ids)) if panel_ids else False)
        .order_by(ForecastSolarSample.collected_at_utc.asc())
        .limit(1)
    )
    if snapshot is None:
        return load_forecast_for_day(session, day_start)
    return list(session.scalars(
        select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == snapshot)
    ).all())


def actual_hourly_from_samples(
    samples: list[SigenStorModbusSample],
    day_start: datetime,
    *,
    fill_missing: bool,
) -> pd.DataFrame:
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    totals: dict[tuple[str, str], float] = {}
    columns = (
        ("solar", "plant_pv_total_kwh_period"),
        ("load", "plant_load_total_kwh_period"),
        ("battery_charge", "plant_battery_charge_total_kwh_period"),
        ("battery_discharge", "plant_battery_discharge_total_kwh_period"),
        ("grid_import", "plant_grid_import_total_kwh_period"),
        ("grid_export", "plant_grid_export_total_kwh_period"),
        ("grid_import_cost", "grid_import_cost_period"),
        ("grid_export_revenue", "grid_export_revenue_period"),
        ("net_cost", "net_cost_period"),
        ("no_solar_battery_import_cost", "no_solar_battery_import_cost_period"),
    )
    rate_columns = (("import_rate", "import_rate"), ("export_rate", "export_rate"))
    rate_totals: dict[tuple[str, str], float] = {}
    rate_counts: dict[tuple[str, str], int] = {}
    tariff_bands: set[str] = set()
    for sample in samples:
        timestamp = parse_time(sample.collected_at_utc, day_start.tzinfo or ZoneInfo("UTC"))
        if timestamp <= day_start:
            continue
        hour = format_hour(timestamp - timedelta(microseconds=1), day_start)
        if hour is None:
            continue
        for name, attribute in columns:
            value = getattr(sample, attribute)
            if value is not None:
                totals[(hour, name)] = totals.get((hour, name), 0.0) + value
        tariff_band = sample.import_tariff_band or "Unknown"
        tariff_column = f"{IMPORT_COST_TARIFF_PREFIX}{tariff_band}"
        tariff_bands.add(tariff_column)
        if sample.grid_import_cost_period is not None:
            totals[(hour, tariff_column)] = totals.get((hour, tariff_column), 0.0) + sample.grid_import_cost_period
        for name, attribute in rate_columns:
            value = getattr(sample, attribute)
            if value is not None:
                rate_totals[(hour, name)] = rate_totals.get((hour, name), 0.0) + value
                rate_counts[(hour, name)] = rate_counts.get((hour, name), 0) + 1

    rows: list[dict[str, float | str]] = []
    for hour in dataframe_column(hours, "hour"):
        row: dict[str, float | str] = {"hour": hour}
        for name, _ in columns:
            row[name] = totals.get((hour, name), float("nan"))
        for name in sorted(tariff_bands):
            row[name] = totals.get((hour, name), float("nan"))
        for name, _ in rate_columns:
            count = rate_counts.get((hour, name), 0)
            row[name] = rate_totals[(hour, name)] / count if count else float("nan")
        row["battery"] = row["battery_charge"] - row["battery_discharge"]
        rows.append(row)
    result = pd.DataFrame(rows).drop(columns=["battery_charge", "battery_discharge"])
    return result.fillna(0.0) if fill_missing else result


def actual_arrays_hourly_from_samples(
    samples: list[SigenStorModbusSample],
    day_start: datetime,
    actuals_to_forecast: dict[str, int],
    *,
    fill_missing: bool,
) -> pd.DataFrame:
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    totals: dict[tuple[str, str], float] = {}
    timezone = day_start.tzinfo or ZoneInfo("UTC")
    for sample in samples:
        timestamp = parse_time(sample.collected_at_utc, timezone)
        if timestamp <= day_start:
            continue
        hour = format_hour(timestamp - timedelta(microseconds=1), day_start)
        if hour is None:
            continue
        for pv_string, panel_id in actuals_to_forecast.items():
            value = getattr(sample, PV_STRING_ENERGY_COLUMNS[pv_string])
            if value is not None:
                column = actual_column_name(panel_id)
                totals[(hour, column)] = totals.get((hour, column), 0.0) + value
    for panel_id in actuals_to_forecast.values():
        column = actual_column_name(panel_id)
        missing = 0.0 if fill_missing else float("nan")
        hours[column] = dataframe_column(hours, "hour").map(lambda hour: totals.get((hour, column), missing))
    return hours


def load_actual_hourly(
    session: Session,
    day_start: datetime,
    *,
    fill_missing: bool = True,
) -> pd.DataFrame:
    next_day = day_start + timedelta(days=1)
    samples = list(session.scalars(
        select(SigenStorHourlySample)
        .where(SigenStorHourlySample.hour_start_utc >= day_start.isoformat())
        .where(SigenStorHourlySample.hour_start_utc < next_day.isoformat())
        .order_by(SigenStorHourlySample.hour_start_utc)
    ).all())
    return actual_hourly_from_rollup_samples(samples, day_start, fill_missing=fill_missing)


def actual_hourly_from_rollup_samples(
    samples: list[SigenStorHourlySample],
    day_start: datetime,
    *,
    fill_missing: bool,
) -> pd.DataFrame:
    """Build current-day totals from stored hourly period aggregates."""
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    totals: dict[tuple[str, str], float] = {}
    rate_totals: dict[tuple[str, str], float] = {}
    rate_counts: dict[tuple[str, str], int] = {}
    tariff_bands: set[str] = set()
    columns = (
        ("solar", "plant_pv_total_kwh_period"),
        ("load", "plant_load_total_kwh_period"),
        ("battery_charge", "plant_battery_charge_total_kwh_period"),
        ("battery_discharge", "plant_battery_discharge_total_kwh_period"),
        ("grid_import", "plant_grid_import_total_kwh_period"),
        ("grid_export", "plant_grid_export_total_kwh_period"),
        ("grid_import_cost", "grid_import_cost_period"),
        ("grid_export_revenue", "grid_export_revenue_period"),
        ("net_cost", "net_cost_period"),
        ("no_solar_battery_import_cost", "no_solar_battery_import_cost_period"),
    )

    timezone = day_start.tzinfo or ZoneInfo("UTC")
    for sample in samples:
        hour = format_hour(parse_time(sample.hour_start_utc, timezone), day_start)
        if hour is None:
            continue
        for name, attribute in columns:
            value = getattr(sample, attribute)
            if value is not None:
                totals[(hour, name)] = totals.get((hour, name), 0.0) + value
        tariff_column = f"{IMPORT_COST_TARIFF_PREFIX}{sample.import_tariff_band}"
        tariff_bands.add(tariff_column)
        if sample.grid_import_cost_period is not None:
            totals[(hour, tariff_column)] = totals.get((hour, tariff_column), 0.0) + sample.grid_import_cost_period
        for name in ("import_rate", "export_rate"):
            value = getattr(sample, name)
            if value is not None:
                key = (hour, name)
                rate_totals[key] = rate_totals.get(key, 0.0) + value
                rate_counts[key] = rate_counts.get(key, 0) + (getattr(sample, HOURLY_VALID_COUNT_COLUMNS[name]) or 0)

    rows: list[dict[str, float | str]] = []
    for hour in dataframe_column(hours, "hour"):
        row: dict[str, float | str] = {"hour": hour}
        for name, _ in columns:
            row[name] = totals.get((hour, name), float("nan"))
        for name in sorted(tariff_bands):
            row[name] = totals.get((hour, name), float("nan"))
        for name in ("import_rate", "export_rate"):
            count = rate_counts.get((hour, name), 0)
            row[name] = rate_totals[(hour, name)] / count if count else float("nan")
        row["battery"] = row["battery_charge"] - row["battery_discharge"]
        rows.append(row)
    result = pd.DataFrame(rows).drop(columns=["battery_charge", "battery_discharge"])
    return result.fillna(0.0) if fill_missing else result


def load_actual_arrays_hourly(
    session: Session,
    day_start: datetime,
    actuals_to_forecast: dict[str, int],
    *,
    fill_missing: bool = True,
) -> pd.DataFrame:
    if day_start.tzinfo is None:
        raise ValueError("day_start must be timezone-aware")
    if not actuals_to_forecast:
        return pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})

    next_day = day_start + timedelta(days=1)
    samples = list(session.scalars(
        select(SigenStorHourlySample)
        .where(SigenStorHourlySample.hour_start_utc >= day_start.isoformat())
        .where(SigenStorHourlySample.hour_start_utc < next_day.isoformat())
        .order_by(SigenStorHourlySample.hour_start_utc)
    ).all())
    return actual_arrays_hourly_from_rollup_samples(samples, day_start, actuals_to_forecast, fill_missing=fill_missing)


def actual_arrays_hourly_from_rollup_samples(
    samples: list[SigenStorHourlySample],
    day_start: datetime,
    actuals_to_forecast: dict[str, int],
    *,
    fill_missing: bool,
) -> pd.DataFrame:
    """Return per-array hourly energy directly from the rollup's period sums."""
    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    totals: dict[tuple[str, str], float] = {}
    timezone = day_start.tzinfo or ZoneInfo("UTC")
    for sample in samples:
        timestamp = parse_time(sample.hour_start_utc, timezone)
        hour = format_hour(timestamp, day_start)
        if hour is None:
            continue
        for pv_string, panel_id in actuals_to_forecast.items():
            value = getattr(sample, PV_STRING_ENERGY_COLUMNS[pv_string])
            if value is not None:
                column = actual_column_name(panel_id)
                totals[(hour, column)] = totals.get((hour, column), 0.0) + value
    for panel_id in actuals_to_forecast.values():
        column = actual_column_name(panel_id)
        missing = 0.0 if fill_missing else float("nan")
        hours[column] = dataframe_column(hours, "hour").map(
            lambda hour, column=column: totals.get((hour, column), missing)
        )
    return hours


def scale_forecast_to_elapsed_time(
    forecast: pd.DataFrame,
    current_time: datetime,
) -> pd.DataFrame:
    elapsed_hour_fraction = (
        current_time.minute * 60
        + current_time.second
        + current_time.microsecond / 1_000_000
    ) / 3_600

    def hour_weight(hour_label: object) -> float:
        hour = int(str(hour_label).split(":", maxsplit=1)[0])
        if hour < current_time.hour:
            return 1.0
        if hour == current_time.hour:
            return elapsed_hour_fraction
        return 0.0

    scaled = forecast.copy()
    weights = dataframe_column(scaled, "hour").map(hour_weight)
    for column in scaled.columns:
        if column.startswith("forecast_"):
            scaled[column] = pd.to_numeric(
                dataframe_column(scaled, column),
                errors="coerce",
            ) * weights
    return scaled


def aggregate_forecast(
    rows: list[ForecastSolarSample],
    day_start: datetime,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    *,
    fill_missing: bool = True,
    source: str = "adjusted",
) -> pd.DataFrame:
    """Aggregate either the deliberate raw or adjusted forecast series."""
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")
    try:
        readers = FORECAST_WATT_HOURS_READERS[source]
    except KeyError as error:
        raise ValueError(f"Unknown forecast source: {source!r}") from error

    points: list[dict[str, object]] = []
    for row in rows:
        try:
            timestamp = parse_time(row.forecast_time, timezone)
        except (TypeError, ValueError):
            continue
        for array in forecast_arrays:
            watt_hours = readers[array.panel_id](row)
            if watt_hours is not None:
                points.append(
                    {
                        "timestamp": timestamp,
                        "panel_id": array.panel_id,
                        "watt_hours": watt_hours,
                    }
                )

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    forecast_columns = [forecast_column_name(array.panel_id) for array in forecast_arrays]
    if not points:
        missing_value = 0.0 if fill_missing else float("nan")
        return hours.assign(
            **{column: missing_value for column in [*forecast_columns, "forecast_total"]}
        )

    frame = pd.DataFrame(points).sort_values(["panel_id", "timestamp"])
    available_panels = set(dataframe_column(frame, "panel_id"))
    frame["forecast_date"] = frame["timestamp"].dt.date
    frame["energy_kwh"] = (
        frame.groupby(["panel_id", "forecast_date"])["watt_hours"]
        .diff()
        .fillna(frame["watt_hours"])
        .clip(lower=0)
        .div(1000)
    )
    # Forecast.Solar timestamps label the start of the predicted hour, the
    # same convention used by the hourly actual rollup.  Do not shift the
    # forecast into the preceding local hour (which is especially visible
    # around sunset and during DST).
    frame["hour"] = frame["timestamp"].map(lambda value: format_hour(value, day_start))
    frame = frame.dropna(subset=["hour"])
    grouped = frame.pivot_table(index="hour", columns="panel_id", values="energy_kwh", aggfunc="sum", fill_value=0).reset_index()
    grouped = grouped.rename(columns={
        array.panel_id: forecast_column_name(array.panel_id) for array in forecast_arrays
    })
    for array in forecast_arrays:
        column = forecast_column_name(array.panel_id)
        if column not in grouped:
            grouped[column] = float("nan")
    result = hours.merge(grouped[["hour", *forecast_columns]], on="hour", how="left")
    for array in forecast_arrays:
        column = forecast_column_name(array.panel_id)
        if array.panel_id in available_panels or fill_missing:
            result[column] = dataframe_column(result, column).fillna(0.0)
    result["forecast_total"] = result[forecast_columns].sum(axis=1, min_count=1)
    return result


def parse_time(value: str, timezone: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def hour_index(value: datetime, day_start: datetime) -> int | None:
    index = int((value - day_start).total_seconds() // 3600)
    return index if 0 <= index < 24 else None


def format_hour(value: datetime, day_start: datetime) -> str | None:
    index = hour_index(value, day_start)
    return f"{index:02d}:00" if index is not None else None


def actual_column_name(panel_id: int) -> str:
    return f"actual_panel_{panel_id}"


def allocate_solar_energy_to_arrays(
    dataframe: pd.DataFrame,
    actuals_to_forecast: dict[str, int],
) -> pd.DataFrame:
    """Allocate measured solar energy to arrays using their DC-power proportions."""
    array_columns = [
        actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()
        if actual_column_name(panel_id) in dataframe
    ]
    if "solar" not in dataframe or not array_columns:
        return dataframe

    allocated = dataframe.copy()
    solar = pd.to_numeric(allocated["solar"], errors="coerce")
    weights = allocated[array_columns].apply(pd.to_numeric, errors="coerce").clip(lower=0).fillna(0.0)
    weight_total = weights.sum(axis=1)
    weighted_rows = solar.notna() & weight_total.gt(0)
    unweighted_rows = solar.notna() & ~weight_total.gt(0)

    for column in array_columns:
        allocated.loc[weighted_rows, column] = (
            solar.loc[weighted_rows]
            * weights.loc[weighted_rows, column]
            / weight_total.loc[weighted_rows]
        )
        allocated.loc[unweighted_rows, column] = (
            solar.loc[unweighted_rows] / len(array_columns)
        )
    return allocated


def forecast_column_name(panel_id: int) -> str:
    return f"forecast_panel_{panel_id}"


def interval_label(value: datetime, interval_minutes: int) -> str:
    minute_of_day = value.hour * 60 + value.minute
    bucket_start = minute_of_day // interval_minutes * interval_minutes
    return minute_of_day_label(bucket_start)


def minute_of_day_label(minute_of_day: int) -> str:
    hour, minute = divmod(minute_of_day, 60)
    return f"{hour:02d}:{minute:02d}"


def quarter_hour_label(value: datetime) -> str:
    minute = value.minute // 15 * 15
    return f"{value.hour:02d}:{minute:02d}"


def battery_status_text(latest: dict[str, float | None]) -> str:
    energy = format_dashboard_number(latest["battery_available_energy_kwh"])
    soc = format_dashboard_number(latest["battery_soc_percent"])
    return f"Battery: {energy} kWh available, {soc}% SoC"


def energy_summary_rows(data: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {
            "period": str(row["period"]),
            **{
                column: (
                    format_currency(row[column])
                    if column in CURRENCY_SUMMARY_COLUMNS
                    else format_dashboard_number(row[column])
                )
                for column in ENERGY_SUMMARY_COLUMNS
            },
        }
        for row in data.to_dict(orient="records")
    ]


def summary_rows(
    data: pd.DataFrame,
    latest: dict[str, float | None],
    latest_keys: dict[str, str] = SUMMARY_LATEST_KEYS,
) -> list[dict[str, str]]:
    today = {
        "solar": float(dataframe_column(data, "solar").sum()),
        "battery": float(dataframe_column(data, "battery").sum()),
        "load": float(dataframe_column(data, "load").sum()),
        "grid_import": float(dataframe_column(data, "grid_import").sum()),
        "grid_export": float(dataframe_column(data, "grid_export").sum()),
    }
    latest_forecast = {
        "solar": float(dataframe_column(
            data, "latest_forecast_total" if "latest_forecast_total" in data else "forecast_total",
        ).sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    # Retain the existing `forecast` field name for the table/API, but its
    # definition is explicitly the beginning-of-day adjusted forecast.
    forecast = {
        "solar": float(dataframe_column(
            data, "forecast_bod_total" if "forecast_bod_total" in data else "forecast_total",
        ).sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    forecast_raw = {
        "solar": float(dataframe_column(data, "forecast_raw_total").sum()),
        "battery": None,
        "inverter": None,
        "load": None,
        "grid_import": None,
        "grid_export": None,
    }
    rows = [
        {
            "metric": label,
            "latest_key": key,
            "is_solar_orientation": key.startswith("pv"),
            "latest": format_dashboard_number(latest.get(key)),
            "today": format_dashboard_number(today.get(key)),
            "latest_forecast": format_dashboard_number(latest_forecast.get(key)),
            "forecast": format_dashboard_number(forecast.get(key)),
            "forecast_raw": format_dashboard_number(forecast_raw.get(key)),
        }
        for label, key in latest_keys.items()
    ]
    return rows


def format_dashboard_number(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    rounded = round(float(value), 1)
    return f"{0.0 if rounded == 0 else rounded:.1f}"


def format_currency(value: float | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.2f}"


def chart_values(values: pd.Series) -> list[float | None]:
    rounded_values: list[float | None] = []
    for value in values:
        if pd.isna(value):
            rounded_values.append(None)
            continue
        rounded = round(float(value), 1)
        rounded_values.append(0.0 if rounded == 0 else rounded)
    return rounded_values


def dataframe_column(frame: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, frame[name])
