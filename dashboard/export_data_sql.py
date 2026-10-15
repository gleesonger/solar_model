"""Native SQLite aggregation and joins for Analysis downloads."""

from __future__ import annotations

from datetime import datetime, timezone as utc_timezone, tzinfo
from time import perf_counter
from typing import TYPE_CHECKING

import pandas as pd

from common import LOGGER
from database import create_engine_for_database

if TYPE_CHECKING:
    from .data import AnalysisRangeBounds


def _timezone_segments(start: int, end: int, zone: tzinfo) -> list[tuple[int, int, int]]:
    """Build a small UTC-offset lookup, not a per-reading Python callback."""
    def offset(stamp: int) -> int:
        return int(datetime.fromtimestamp(stamp, zone).utcoffset().total_seconds())

    segments = []
    segment_start, previous, current_offset = start, start, offset(start)
    while previous < end - 1:
        probe = min(previous + 3600, end - 1)
        next_offset = offset(probe)
        if next_offset != current_offset:
            low, high = previous + 1, probe
            while low < high:
                middle = (low + high) // 2
                if offset(middle) == current_offset:
                    low = middle + 1
                else:
                    high = middle
            segments.append((segment_start, low, current_offset))
            segment_start, current_offset = low, next_offset
        previous = probe
    segments.append((segment_start, end, current_offset))
    return segments


def _period_sql(stamp: str, timestep: str) -> str:
    """Bucket timestamps natively; calendar keys are local-wall-time epochs."""
    local = f"({stamp} + z.utc_offset)"
    if timestep in {"minute", "hour"}:
        pattern = "%Y-%m-%d %H:%M:00" if timestep == "minute" else "%Y-%m-%d %H:00:00"
        return f"CAST(strftime('%s', strftime('{pattern}', {local}, 'unixepoch')) AS INTEGER) - z.utc_offset"
    modifiers = {
        "day": "'start of day'",
        "week": "'start of day', '-' || ((CAST(strftime('%w', " + local + ", 'unixepoch') AS INTEGER) + 6) % 7) || ' days'",
        "month": "'start of month'",
        "year": "'start of year'",
        "season": "'start of month', '-' || (CAST(strftime('%m', " + local + ", 'unixepoch') AS INTEGER) % 3) || ' months'",
    }
    return f"CAST(strftime('%s', {local}, 'unixepoch', {modifiers[timestep]}) AS INTEGER)"


def load_analysis_export_dataframe(
    database_path: str, bounds: AnalysisRangeBounds, timestep: str, timezone: tzinfo, *, include_forecast: bool,
) -> pd.DataFrame:
    """Read the fully aggregated, joined result directly into pandas."""
    from .data import (
        FORECAST_EXPORT_COLUMNS, SAMPLE_EXPORT_COLUMNS, analysis_interval_start,
        export_aggregation, export_field_label,
    )

    analysis_interval_start(bounds.start_at, timestep)
    parameters = {
        "start": round(bounds.start_at.timestamp()),
        "end": round(bounds.end_at.timestamp()),
        "target_start": bounds.start_at.timestamp(),
        "target_end": bounds.end_at.timestamp(),
    }
    offsets = _timezone_segments(parameters["start"], parameters["end"] + 1, timezone)
    values = ", ".join(f"({start}, {end}, {offset})" for start, end, offset in offsets)
    ctes = [f"offsets(utc_start, utc_end, utc_offset) AS (VALUES {values})"]

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def add_dataset(prefix: str, columns: tuple[str, ...], *, forecast: bool) -> None:
        stamp = "f.forecast_time" if forecast else "s.collected_at_utc"
        bucket = _period_sql(stamp, timestep)
        if forecast:
            ctes.extend([
                """collections AS (
                    SELECT DISTINCT collected_at_utc FROM forecast_solar
                    WHERE collected_at_utc >= :start AND collected_at_utc <= :end
                )""",
                """first_collections AS (
                    SELECT MIN(c.collected_at_utc) AS collected_at_utc FROM collections c
                    JOIN offsets z ON c.collected_at_utc >= z.utc_start AND c.collected_at_utc < z.utc_end
                    GROUP BY date(c.collected_at_utc + z.utc_offset, 'unixepoch')
                )""",
                f"""{prefix}_source AS MATERIALIZED (
                    SELECT f.*, {bucket} AS period,
                        ROW_NUMBER() OVER (ORDER BY f.collected_at_utc, f.forecast_time) AS reading_order
                    FROM forecast_solar f JOIN first_collections c ON f.collected_at_utc = c.collected_at_utc
                    JOIN offsets z ON f.forecast_time >= z.utc_start AND f.forecast_time < z.utc_end
                    WHERE f.forecast_time >= :target_start AND f.forecast_time <= :target_end
                )""",
            ])
            order, last_table, last_key = "reading_order", f"{prefix}_source", "reading_order"
        else:
            ctes.append(f"""{prefix}_source AS MATERIALIZED (
                SELECT s.*, {bucket} AS period FROM sigenstor s
                JOIN offsets z ON s.collected_at_utc >= z.utc_start AND s.collected_at_utc < z.utc_end
                WHERE s.collected_at_utc >= :start AND s.collected_at_utc <= :end
            )""")
            order, last_table, last_key = "collected_at_utc", "sigenstor", "collected_at_utc"

        aggregates, projections, joins = [], [], []
        for index, column in enumerate(columns):
            field = quoted(column)
            kind = export_aggregation(column)
            if kind == "last":
                key = quoted(f"last_{index}")
                alias = f"last_{index}"
                aggregates.append(f"MAX(CASE WHEN {field} IS NOT NULL THEN {order} END) AS {key}")
                joins.append(f"LEFT JOIN {last_table} {alias} ON {alias}.{last_key} = g.{key}")
                projections.append(f"{alias}.{field} / 10000.0 AS {field}")
            elif kind == "labels":
                ctes.extend([
                    f"""{prefix}_first_labels AS (
                        SELECT period, {field} AS label, MIN({order}) AS first_reading
                        FROM {prefix}_source WHERE {field} IS NOT NULL GROUP BY period, {field}
                    )""",
                    f"""{prefix}_labels AS (
                        SELECT DISTINCT period, GROUP_CONCAT(label, ' / ') OVER (
                            PARTITION BY period ORDER BY first_reading
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        ) AS labels FROM {prefix}_first_labels
                    )""",
                ])
                joins.append(f"LEFT JOIN {prefix}_labels labels ON labels.period = g.period")
                projections.append(f"labels.labels AS {field}")
            else:
                operation = "SUM" if kind == "sum" else "AVG"
                aggregates.append(f"{operation}({field}) / 10000.0 AS {field}")
                projections.append(f"g.{field}")
        ctes.extend([
            f"{prefix}_grouped AS (SELECT period, " + ", ".join(aggregates)
            + f" FROM {prefix}_source GROUP BY period)",
            f"{prefix}_values AS (SELECT g.period, " + ", ".join(projections)
            + f" FROM {prefix}_grouped g " + " ".join(joins) + ")",
        ])

    add_dataset("actual", SAMPLE_EXPORT_COLUMNS, forecast=False)
    output = [f"a.{quoted(column)} AS {quoted(export_field_label(column))}" for column in SAMPLE_EXPORT_COLUMNS]
    if include_forecast:
        add_dataset("forecast", FORECAST_EXPORT_COLUMNS, forecast=True)
        ctes.append("periods AS (SELECT period FROM actual_values UNION SELECT period FROM forecast_values)")
        output.extend(
            f"f.{quoted(column)} AS {quoted(export_field_label(column, forecast=True))}"
            for column in FORECAST_EXPORT_COLUMNS
        )
        source = "periods p LEFT JOIN actual_values a ON a.period = p.period LEFT JOIN forecast_values f ON f.period = p.period"
        period_column = "p.period"
    else:
        source, period_column = "actual_values a", "a.period"
    sql = "WITH " + ",\n".join(ctes) + f" SELECT {period_column} AS period, " + ", ".join(output)
    sql += f" FROM {source} ORDER BY {period_column}"

    engine = create_engine_for_database(database_path)
    started = perf_counter()
    try:
        with engine.connect() as connection:
            frame = pd.read_sql_query(sql, connection, params=parameters)
    finally:
        engine.dispose()
    LOGGER.info("Download native SQL query and pandas read (%.3fs, %s output rows)", perf_counter() - started, len(frame))
    if not frame.empty:
        if timestep in {"minute", "hour"}:
            frame["period"] = pd.to_datetime(frame["period"], unit="s", utc=True).dt.tz_convert(timezone)
        else:
            # Only final calendar buckets need a local-midnight timezone assignment.
            frame["period"] = pd.DatetimeIndex([
                datetime.fromtimestamp(stamp, utc_timezone.utc).replace(tzinfo=timezone)
                for stamp in frame["period"]
            ])
    return frame
