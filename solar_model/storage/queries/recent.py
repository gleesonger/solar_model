"""Native SQLite energy summaries shared by the Recent tables."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from solar_model.config import SolarArrayConfig
from solar_model.storage.connection import create_engine_for_database
from solar_model.storage.queries.exports import _timezone_segments


ACTUAL_COLUMNS = {
    "solar_actual": "plant_pv_total_kwh_period",
    "load": "plant_load_total_kwh_period",
    "house_load": "house_load_kwh_period",
    "grid_import": "plant_grid_import_total_kwh_period",
    "grid_export": "plant_grid_export_total_kwh_period",
    "grid_import_cost": "grid_import_cost_period",
    "grid_export_revenue": "grid_export_revenue_period",
    "net_cost": "net_cost_period",
    "no_solar_battery_import_cost": "no_solar_battery_import_cost_period",
}


def load_energy_summaries(
    database_path: str,
    timezone_name: str,
    start_date: date,
    end_date: date,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    current_time: datetime,
    *,
    daily_start: date | None = None,
    monthly_start: date | None = None,
    lifetime_start: date | None = None,
    include_totals: bool = True,
) -> pd.DataFrame:
    """Aggregate source measurements once; return only requested table rows.

    Calendar boundaries and UTC offsets are prepared per day/hour, never per
    measurement. Source reads, forecast differences, weighting and totals are SQL.
    """
    from solar_model.storage.queries.dashboard import ENERGY_SUMMARY_COLUMNS

    if end_date < start_date:
        raise ValueError("End date must not be before first date")
    panels = list(dict.fromkeys(array.panel_id for array in forecast_arrays))
    if any(panel not in {1, 2, 3, 4} for panel in panels):
        raise ValueError("Forecast panel ID must be between 1 and 4")
    zone = ZoneInfo(timezone_name)
    calendar = []
    for offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=offset)
        begin = int(datetime.combine(day, time.min, zone).timestamp())
        finish = int(datetime.combine(day + timedelta(days=1), time.min, zone).timestamp())
        calendar.append((day.isoformat(), begin, finish))
    calendar_sql = ",".join(f"('{day}',{begin},{finish})" for day, begin, finish in calendar)
    # Eligible forecast labels are the first 24 elapsed hours after midnight.
    # Cover the following date too for spring DST (which has only 23 hours).
    offsets = _timezone_segments(calendar[0][1], calendar[-1][2] + 86400, zone)
    offset_sql = ",".join(f"({begin},{finish},{offset})" for begin, finish, offset in offsets)
    actual = ",".join(f"SUM(s.{column}) / 10000.0 AS {name}" for name, column in ACTUAL_COLUMNS.items())
    points = " UNION ALL ".join(
        f"""SELECT c.day, c.start, {panel} AS panel, f.forecast_time AS stamp,
            date(f.forecast_time + z.utc_offset, 'unixepoch') AS target_day,
            f.panel_{panel}_adj_watt_hours AS cumulative
        FROM first_collections c JOIN forecast_solar f ON f.collected_at_utc = c.collected
        LEFT JOIN offsets z ON f.forecast_time >= z.lo AND f.forecast_time < z.hi
        WHERE f.panel_{panel}_adj_watt_hours IS NOT NULL"""
        for panel in panels
    ) or "SELECT NULL day, NULL start, NULL panel, NULL stamp, NULL target_day, NULL cumulative WHERE 0"
    parameters = {
        "now": current_time.timestamp(),
        "today": current_time.date().isoformat(),
        "hour": current_time.hour,
        "fraction": (current_time.minute * 60 + current_time.second + current_time.microsecond / 1e6) / 3600,
        "daily_start": daily_start.isoformat() if daily_start else None,
        "monthly_start": monthly_start.isoformat() if monthly_start else None,
        "lifetime_start": lifetime_start.isoformat() if lifetime_start else None,
    }
    ctes = f"""WITH calendar(day,start,end) AS (VALUES {calendar_sql}),
    offsets(lo,hi,utc_offset) AS (VALUES {offset_sql}),
    actual AS (
        SELECT c.day, {actual} FROM calendar c JOIN sigenstor_hourly s
        ON s.hour_start_utc >= c.start AND s.hour_start_utc < c.end GROUP BY c.day
    ),
    first_collections AS (
        SELECT c.day, c.start, MIN(f.collected_at_utc) AS collected FROM calendar c
        JOIN forecast_solar f ON f.collected_at_utc >= c.start AND f.collected_at_utc < c.end GROUP BY c.day
    ),
    points AS MATERIALIZED ({points}),
    differences AS (
        SELECT *, LAG(cumulative) OVER (
            PARTITION BY day, panel, target_day ORDER BY stamp
        ) AS previous FROM points
    ),
    energy AS (
        SELECT day, stamp >= start AND stamp < start + 86400 AS eligible,
            (stamp - start) / 3600 AS hour,
            MAX(cumulative - COALESCE(previous, 0), 0) / 10000000.0 AS value
        FROM differences
    ),
    forecast AS (
        SELECT day, SUM(CASE WHEN eligible THEN value *
            CASE WHEN day <> :today OR hour < :hour THEN 1
                 WHEN hour = :hour THEN :fraction ELSE 0 END
            ELSE 0 END) AS solar_forecast FROM energy GROUP BY day
    ),
    daily AS MATERIALIZED (
        SELECT c.day AS date, {','.join('a.' + name for name in ACTUAL_COLUMNS)}, f.solar_forecast
        FROM calendar c LEFT JOIN actual a ON a.day = c.day LEFT JOIN forecast f ON f.day = c.day
    )"""
    fields = ",".join(ENERGY_SUMMARY_COLUMNS)
    sums = ",".join(f"SUM({column}) AS {column}" for column in ENERGY_SUMMARY_COLUMNS)
    outputs = []
    if daily_start is not None:
        outputs.append(f"SELECT 'daily' AS kind, date, NULL AS label, 0 AS position, {fields} FROM daily WHERE date >= :daily_start")
        if include_totals:
            outputs.append(f"SELECT 'daily', NULL, 'Total', 1, {sums} FROM daily WHERE date >= :daily_start")
    if monthly_start is not None:
        outputs.extend([
            f"SELECT 'monthly' AS kind, substr(date,1,7) AS date, NULL AS label, 0 AS position, {sums} FROM daily WHERE date >= :monthly_start GROUP BY substr(date,1,7)",
            f"SELECT 'monthly', NULL, 'Total', 1, {sums} FROM daily WHERE date >= :monthly_start",
            f"SELECT 'monthly', NULL, 'Lifetime', 2, {sums} FROM daily WHERE date >= :lifetime_start",
        ])
    if not outputs:
        raise ValueError("At least one summary table must be requested")
    sql = ctes + f" SELECT kind, date, label, {fields} FROM (" + " UNION ALL ".join(outputs) + ") ORDER BY kind, position, date DESC"
    engine = create_engine_for_database(database_path)
    try:
        with engine.connect() as connection:
            return pd.read_sql_query(sql, connection, params=parameters)
    finally:
        engine.dispose()
