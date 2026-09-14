"""Data access and calculations shared by dashboard tabs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from common import heavy_work
from config import SolarArrayConfig
from database import ForecastSolarSample, SigenStorDevice, SigenStorModbusSample, create_engine_for_database
from .models import (
    AnalysisRangeData,
    HOURLY_PERIOD_DAYS,
    HOURLY_PERIODS,
    HourlyRangeData,
    SUMMARY_LATEST_KEYS,
    TelemetryData,
)

PvStringReader = Callable[[SigenStorModbusSample], tuple[float | None, float | None]]
ForecastWattHoursReader = Callable[[ForecastSolarSample], float | None]

PV_STRING_READERS: dict[str, PvStringReader] = {
    "pv1": lambda row: (row.inverter_pv1_voltage_volts, row.inverter_pv1_current_amps),
    "pv2": lambda row: (row.inverter_pv2_voltage_volts, row.inverter_pv2_current_amps),
    "pv3": lambda row: (row.inverter_pv3_voltage_volts, row.inverter_pv3_current_amps),
    "pv4": lambda row: (row.inverter_pv4_voltage_volts, row.inverter_pv4_current_amps),
}

FORECAST_WATT_HOURS_READERS: dict[int, ForecastWattHoursReader] = {
    1: lambda row: row.panel_1_watt_hours,
    2: lambda row: row.panel_2_watt_hours,
    3: lambda row: row.panel_3_watt_hours,
    4: lambda row: row.panel_4_watt_hours,
}
IMPORT_COST_TARIFF_PREFIX = "import_cost_tariff__"
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
            forecast_rows = load_forecast_for_day(session, day_start)
            actual = load_actual_hourly(session, day_start)
            actual_by_array = load_actual_arrays_hourly(session, day_start, actuals_to_forecast)
    finally:
        engine.dispose()

    forecast = aggregate_forecast(forecast_rows, day_start, forecast_arrays)
    hourly = actual.merge(actual_by_array, on="hour", how="left").merge(forecast, on="hour", how="left")
    hourly = allocate_solar_energy_to_arrays(hourly, actuals_to_forecast).fillna(0.0)
    return hourly


def load_grid_energy_day_start(
    database_path: str,
    local_date: str,
) -> dict[str, float]:
    """Return the day's opening lifetime grid meters for live daily totals."""
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            row = session.execute(
                select(
                    SigenStorModbusSample.plant_grid_import_total_kwh,
                    SigenStorModbusSample.plant_grid_export_total_kwh,
                )
                .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) == local_date)
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


@heavy_work("dashboard recent daily energy summary")
def load_recent_daily_energy(
    database_path: str,
    timezone_name: str,
    end_date: date,
    days: int,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    if days < 1:
        raise ValueError("Recent daily period must contain at least one day")
    start_date = end_date - timedelta(days=days - 1)
    daily = load_daily_energy_totals(
        database_path,
        timezone_name,
        start_date,
        end_date,
        forecast_arrays,
    )
    daily["period"] = dataframe_column(daily, "date")
    ordered = daily.sort_values("date", ascending=False).reset_index(drop=True)
    return append_energy_total(ordered, "Total")


def load_lifetime_start_date(database_path: str, fallback: date) -> date:
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            actual_start = session.scalar(
                select(func.min(func.substr(
                    SigenStorModbusSample.collected_at_local,
                    1,
                    10,
                )))
            )
            forecast_start = session.scalar(
                select(func.min(func.substr(
                    ForecastSolarSample.collected_at_local,
                    1,
                    10,
                )))
            )
    finally:
        engine.dispose()

    starts = [
        date.fromisoformat(str(value))
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
    if months < 1:
        raise ValueError("Recent monthly period must contain at least one month")
    current_month = end_date.replace(day=1)
    start_date = shift_date_by_months(current_month, -(months - 1))
    daily = load_daily_energy_totals(
        database_path,
        timezone_name,
        start_date,
        end_date,
        forecast_arrays,
    )
    value_columns = list(ENERGY_SUMMARY_COLUMNS)
    daily["month"] = dataframe_column(daily, "date").map(
        lambda value: str(value)[:7]
    )
    totals = cast(
        pd.DataFrame,
        daily.groupby("month", as_index=False)[value_columns].sum(min_count=1),
    )
    rows: list[dict[str, Any]] = []
    for months_ago in range(months):
        month = shift_date_by_months(current_month, -months_ago)
        month_key = month.strftime("%Y-%m")
        matching = totals.loc[dataframe_column(totals, "month") == month_key]
        row: dict[str, Any] = {
            "date": month_key,
            "period": month.strftime("%b %Y"),
        }
        for column in value_columns:
            row[column] = (
                dataframe_column(matching, column).iloc[0]
                if not matching.empty
                else float("nan")
            )
        rows.append(row)
    recent = append_energy_total(pd.DataFrame(rows), "Total")
    lifetime_start = load_lifetime_start_date(database_path, end_date)
    lifetime = load_daily_energy_totals(
        database_path,
        timezone_name,
        lifetime_start,
        end_date,
        forecast_arrays,
    )
    return append_energy_total(recent, "Lifetime", source=lifetime)


@heavy_work("dashboard daily energy aggregation")
def load_daily_energy_totals(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
) -> pd.DataFrame:
    if end_date < start_date:
        raise ValueError("End date must not be before first date")

    timezone = ZoneInfo(timezone_name)
    current_time = datetime.now(timezone)
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            actual_start = datetime.combine(start_date, time.min, tzinfo=timezone)
            actual_end = datetime.combine(
                end_date + timedelta(days=1),
                time.min,
                tzinfo=timezone,
            )
            actual_samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(SigenStorModbusSample.collected_at_utc >= actual_start.isoformat())
                .where(SigenStorModbusSample.collected_at_utc < actual_end.isoformat())
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())

            actual_totals: dict[str, dict[str, float]] = {}
            actual_columns = (
                ("solar_actual", "plant_pv_total_kwh_period"),
                ("load", "plant_load_total_kwh_period"),
                ("grid_import", "plant_grid_import_total_kwh_period"),
                ("grid_export", "plant_grid_export_total_kwh_period"),
                ("grid_import_cost", "grid_import_cost_period"),
                ("grid_export_revenue", "grid_export_revenue_period"),
                ("net_cost", "net_cost_period"),
                ("no_solar_battery_import_cost", "no_solar_battery_import_cost_period"),
            )
            for sample in actual_samples:
                actual_date = parse_time(sample.collected_at_utc, timezone).date().isoformat()
                totals = actual_totals.setdefault(actual_date, {})
                for output_name, attribute in actual_columns:
                    value = getattr(sample, attribute)
                    if value is not None:
                        totals[output_name] = totals.get(output_name, 0.0) + value
            actual_rows = [
                {"date": actual_date, **totals}
                for actual_date, totals in actual_totals.items()
            ]

            forecast_date = func.substr(
                ForecastSolarSample.collected_at_local,
                1,
                10,
            ).label("date")
            collections = session.execute(
                select(
                    forecast_date,
                    ForecastSolarSample.collected_at_utc,
                    ForecastSolarSample.collected_at_utc,
                )
                .where(ForecastSolarSample.collected_at_utc.is_not(None))
                .where(forecast_date >= start_text)
                .where(forecast_date <= end_text)
                .distinct()
                .order_by(forecast_date, ForecastSolarSample.collected_at_utc)
            ).all()
            first_guid_by_date: dict[str, str] = {}
            for collection_date, collection_timestamp, _ in collections:
                if collection_timestamp is not None:
                    first_guid_by_date.setdefault(
                        str(collection_date),
                        str(collection_timestamp),
                    )
            selected_guids = set(first_guid_by_date.values())
            forecast_rows = (
                list(session.scalars(
                    select(ForecastSolarSample)
                    .where(ForecastSolarSample.collected_at_utc.in_(selected_guids))
                    .order_by(ForecastSolarSample.forecast_time)
                ).all())
                if selected_guids
                else []
            )
    finally:
        engine.dispose()

    actual_by_date = {str(row["date"]): row for row in actual_rows}
    forecast_rows_by_guid: dict[str, list[ForecastSolarSample]] = {}
    for forecast_row in forecast_rows:
        if forecast_row.collected_at_utc is not None:
            forecast_rows_by_guid.setdefault(
                forecast_row.collected_at_utc,
                [],
            ).append(forecast_row)

    rows: list[dict[str, Any]] = []
    number_of_days = (end_date - start_date).days + 1
    for day_offset in range(number_of_days):
        selected_date = start_date + timedelta(days=day_offset)
        selected_text = selected_date.isoformat()
        actual = actual_by_date.get(selected_text, {})
        day_start = datetime(
            selected_date.year,
            selected_date.month,
            selected_date.day,
            tzinfo=timezone,
        )
        selected_guid = first_guid_by_date.get(selected_text)
        selected_forecast_rows = (
            forecast_rows_by_guid.get(selected_guid, [])
            if selected_guid is not None
            else []
        )
        forecast = aggregate_forecast(
            selected_forecast_rows,
            day_start,
            forecast_arrays,
            fill_missing=False,
        )
        if selected_date == current_time.date():
            forecast = scale_forecast_to_elapsed_time(forecast, current_time)
        rows.append({
            "date": selected_text,
            "solar_actual": actual.get("solar_actual"),
            "solar_forecast": dataframe_column(
                forecast,
                "forecast_total",
            ).sum(min_count=1),
            "load": actual.get("load"),
            "grid_import": actual.get("grid_import"),
            "grid_export": actual.get("grid_export"),
            "grid_import_cost": actual.get("grid_import_cost"),
            "grid_export_revenue": actual.get("grid_export_revenue"),
            "net_cost": actual.get("net_cost"),
            "no_solar_battery_import_cost": actual.get(
                "no_solar_battery_import_cost"
            ),
        })
    return pd.DataFrame(rows)


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
            selected_samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    >= start_date.isoformat()
                )
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    <= end_date.isoformat()
                )
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
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
) -> AnalysisSourceData:
    """Load the raw records shared by Analysis charts and downloads."""
    engine = create_engine_for_database(database_path)
    try:
        with Session(engine) as session:
            samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(SigenStorModbusSample.collected_at_utc >= bounds.start_at.isoformat())
                .where(SigenStorModbusSample.collected_at_utc <= bounds.end_at.isoformat())
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
            forecast_rows = list(session.scalars(
                select(ForecastSolarSample)
                .where(ForecastSolarSample.collected_at_utc >= bounds.start_at.isoformat())
                .where(ForecastSolarSample.collected_at_utc <= bounds.end_at.isoformat())
                .order_by(ForecastSolarSample.collected_at_utc, ForecastSolarSample.forecast_time)
            ).all())
    finally:
        engine.dispose()
    return AnalysisSourceData(samples=samples, forecast_rows=forecast_rows)


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
    """Aggregate Analysis records into chronological export intervals."""
    source = load_analysis_source_data(database_path, bounds)
    actual_records = [{
        "period": analysis_interval_start(parse_time(sample.collected_at_utc, timezone), timestep),
        **{column: getattr(sample, column) for column in SAMPLE_EXPORT_COLUMNS},
    } for sample in source.samples]
    actual = aggregate_analysis_export_records(
        actual_records,
        columns=SAMPLE_EXPORT_COLUMNS,
    )
    if not include_forecast:
        return actual
    forecast = aggregate_analysis_export_records(
        analysis_forecast_export_records(
            source.forecast_rows, bounds, timestep, timezone,
        ),
        columns=FORECAST_EXPORT_COLUMNS,
        forecast=True,
    )
    return actual.merge(forecast, on="period", how="outer").sort_values("period").reset_index(drop=True)


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
    """Load every Analysis dataset through one database session and date range."""
    if power_interval_minutes < 1:
        raise ValueError("Power interval must be at least one minute")

    timezone = ZoneInfo(timezone_name)
    source = load_analysis_source_data(database_path, bounds)

    daily_frames = analysis_daily_energy_frames(
        source.samples,
        source.forecast_rows,
        timezone,
        bounds.start_date,
        bounds.end_date,
        forecast_arrays,
        actuals_to_forecast,
    )
    energy = historical_energy_from_frames(
        daily_frames,
        frequency,
        bounds.start_at,
        bounds.end_at,
        aggregation=aggregation,
    )
    power, _ = average_telemetry_by_interval(
        source.samples,
        timezone,
        power_interval_minutes,
        actuals_to_forecast,
    )
    # Battery is deliberately fixed at an hourly view.  Unlike the power
    # chart, it does not respond to the power chart's zoom level.
    _, battery = average_telemetry_by_interval(
        source.samples,
        timezone,
        60,
        actuals_to_forecast,
    )
    return AnalysisRangeData(energy=energy, power=power, battery=battery)


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
        )
        frame = actual.merge(actual_by_array, on="hour", how="left").merge(forecast, on="hour", how="left")
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
            samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) >= start_date.isoformat())
                .where(func.substr(SigenStorModbusSample.collected_at_local, 1, 10) <= end_date.isoformat())
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
    finally:
        engine.dispose()

    array_columns = [
        *(actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()),
        *(forecast_column_name(array.panel_id) for array in forecast_arrays),
    ]
    value_columns = [
        "solar",
        "forecast_total",
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
        forecast_rows = (
            load_forecast_for_day(session, day_start)
            if selected_date_text in forecast_dates
            else []
        )
        forecast = aggregate_forecast(
            forecast_rows, day_start, forecast_arrays, fill_missing=False
        )
        day = actual.merge(actual_by_array, on="hour", how="left").merge(
            forecast, on="hour", how="left"
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
            samples = list(session.scalars(
                select(SigenStorModbusSample)
                .where(
                    func.substr(SigenStorModbusSample.collected_at_local, 1, 10)
                    == day_start.strftime("%Y-%m-%d")
                )
                .order_by(SigenStorModbusSample.collected_at_utc)
            ).all())
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
            "available_energy_kwh": sample.inverter_battery_available_discharge_kwh,
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
        "inverter_today": latest_sample.inverter_pv_daily_kwh if latest_sample is not None else None,
        "load": latest_sample.plant_load_power_kw if latest_sample is not None else None,
        "grid_import": max(latest_grid, 0.0) if latest_grid is not None else None,
        "grid_export": max(-latest_grid, 0.0) if latest_grid is not None else None,
        "battery_available_energy_kwh": (
            latest_sample.inverter_battery_available_discharge_kwh
            if latest_sample is not None
            else None
        ),
        "battery_soc_percent": (
            (
                latest_sample.plant_battery_soc_percent
                if latest_sample.plant_battery_soc_percent is not None
                else latest_sample.inverter_battery_soc_percent
            )
            if latest_sample is not None
            else None
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
            "available_energy_kwh": sample.inverter_battery_available_discharge_kwh,
            "soc_percent": sample.plant_battery_soc_percent,
        }
        for pv_string, panel_id in actuals_to_forecast.items():
            voltage, current = PV_STRING_READERS[pv_string](sample)
            values[actual_column_name(panel_id)] = (
                max(voltage * current, 0.0) / 1000
                if voltage is not None and current is not None
                else None
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
    next_day = day_start + timedelta(days=1)
    first_forecast = session.scalar(
        select(ForecastSolarSample.collected_at_utc)
        .where(ForecastSolarSample.collected_at_utc >= day_start.isoformat())
        .where(ForecastSolarSample.collected_at_utc < next_day.isoformat())
        .order_by(ForecastSolarSample.collected_at_utc.asc())
        .limit(1)
    )
    if not first_forecast:
        return []
    return list(session.scalars(
        select(ForecastSolarSample).where(ForecastSolarSample.collected_at_utc == first_forecast)
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
    previous_timestamp: datetime | None = None
    previous_power: dict[str, float | None] = {}
    timezone = day_start.tzinfo or ZoneInfo("UTC")
    for sample in samples:
        timestamp = parse_time(sample.collected_at_utc, timezone)
        current_power: dict[str, float | None] = {}
        for pv_string in actuals_to_forecast:
            voltage, current = PV_STRING_READERS[pv_string](sample)
            current_power[pv_string] = max(voltage * current, 0.0) / 1000 if voltage is not None and current is not None else None
        if previous_timestamp is not None:
            duration_hours = (timestamp - previous_timestamp).total_seconds() / 3600
            hour = format_hour(timestamp - timedelta(microseconds=1), day_start)
            if hour is not None and duration_hours > 0:
                for pv_string, panel_id in actuals_to_forecast.items():
                    before, after = previous_power.get(pv_string), current_power[pv_string]
                    if before is not None and after is not None:
                        column = actual_column_name(panel_id)
                        totals[(hour, column)] = totals.get((hour, column), 0.0) + (before + after) / 2 * duration_hours
        previous_timestamp, previous_power = timestamp, current_power
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
        select(SigenStorModbusSample)
        .where(SigenStorModbusSample.collected_at_utc > day_start.isoformat())
        .where(SigenStorModbusSample.collected_at_utc < next_day.isoformat())
        .order_by(SigenStorModbusSample.collected_at_utc)
    ).all())
    return actual_hourly_from_samples(samples, day_start, fill_missing=fill_missing)


def load_actual_arrays_hourly(
    session: Session,
    day_start: datetime,
    actuals_to_forecast: dict[str, int],
    *,
    fill_missing: bool = True,
) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    hours = pd.DataFrame({"hour": [f"{index:02d}:00" for index in range(24)]})
    actual_columns = {actual_column_name(panel_id) for panel_id in actuals_to_forecast.values()}
    if not actuals_to_forecast:
        return hours

    next_day = day_start + timedelta(days=1)
    samples = list(session.scalars(
        select(SigenStorModbusSample)
        .where(SigenStorModbusSample.collected_at_utc >= day_start.isoformat())
        .where(SigenStorModbusSample.collected_at_utc < next_day.isoformat())
        .order_by(SigenStorModbusSample.collected_at_utc)
    ).all())
    totals: dict[tuple[str, str], float] = {}
    previous_timestamp: datetime | None = None
    previous_power: dict[str, float | None] = {}

    for sample in samples:
        timestamp = parse_time(sample.collected_at_utc, timezone)
        current_power: dict[str, float | None] = {}
        for pv_string in actuals_to_forecast:
            voltage, current = PV_STRING_READERS[pv_string](sample)
            current_power[pv_string] = (
                max(voltage * current, 0.0) / 1000
                if voltage is not None and current is not None
                else None
            )

        if previous_timestamp is not None:
            duration_hours = (timestamp - previous_timestamp).total_seconds() / 3600
            hour = format_hour(timestamp - timedelta(microseconds=1), day_start)
            if hour is not None and duration_hours > 0:
                for pv_string, panel_id in actuals_to_forecast.items():
                    before = previous_power.get(pv_string)
                    after = current_power[pv_string]
                    if before is not None and after is not None:
                        column = actual_column_name(panel_id)
                        totals[(hour, column)] = totals.get((hour, column), 0.0) + (
                            (before + after) / 2 * duration_hours
                        )

        previous_timestamp = timestamp
        previous_power = current_power

    for column in actual_columns:
        missing_value = 0.0 if fill_missing else float("nan")
        hours[column] = dataframe_column(hours, "hour").map(
            lambda hour, column=column: totals.get((hour, column), missing_value)
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
) -> pd.DataFrame:
    timezone = day_start.tzinfo
    if timezone is None:
        raise ValueError("day_start must be timezone-aware")

    points: list[dict[str, object]] = []
    for row in rows:
        try:
            timestamp = parse_time(row.forecast_time, timezone)
        except (TypeError, ValueError):
            continue
        for array in forecast_arrays:
            watt_hours = FORECAST_WATT_HOURS_READERS[array.panel_id](row)
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
    frame["hour"] = frame["timestamp"].map(
        lambda value: format_hour(value - timedelta(microseconds=1), day_start)
    )
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
) -> list[dict[str, str]]:
    today = {
        "solar": float(dataframe_column(data, "solar").sum()),
        "battery": float(dataframe_column(data, "battery").sum()),
        "inverter": latest.get("today_inverter", latest.get("inverter_today")),
        "load": float(dataframe_column(data, "load").sum()),
        "grid_import": float(dataframe_column(data, "grid_import").sum()),
        "grid_export": float(dataframe_column(data, "grid_export").sum()),
    }
    forecast = {
        "solar": float(dataframe_column(data, "forecast_total").sum()),
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
            "latest": format_dashboard_number(latest[key]),
            "today": format_dashboard_number(today[key]),
            "forecast": format_dashboard_number(forecast[key]),
        }
        for label, key in SUMMARY_LATEST_KEYS.items()
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
