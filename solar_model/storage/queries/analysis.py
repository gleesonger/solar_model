"""Shared, selection-aware SQLite aggregation for Analysis and custom charts."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from math import ceil, floor
import json
import re
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from solar_model.config import SolarArrayConfig
from solar_model.storage.schema import ForecastSolarSample, SigenStorHourlySample, SigenStorModbusSample
from solar_model.storage.connection import create_engine_for_database


ACTUAL_COLUMNS = {
    "solar": "plant_pv_total_kwh_period",
    "load": "plant_load_total_kwh_period",
    "house_load": "house_load_kwh_period",
    "grid_import": "plant_grid_import_total_kwh_period",
    "grid_export": "plant_grid_export_total_kwh_period",
    "grid_import_cost": "grid_import_cost_period",
    "grid_export_revenue": "grid_export_revenue_period",
    "net_cost": "net_cost_period",
    "no_solar_battery_import_cost": "no_solar_battery_import_cost_period",
}
TELEMETRY_COLUMNS = {
    "power_solar": "plant_pv_power_kw",
    "power_load": "plant_load_power_kw",
    "power_battery": "plant_battery_power_kw",
    "power_inverter": "inverter_power_kw",
    "battery_level": "plant_battery_available_discharge_kwh",
    "soc_percent": "plant_battery_soc_percent",
}


class _CompensatedSum:
    """Match pandas' compensated group sums at display-rounding boundaries."""

    def __init__(self) -> None:
        self.total = 0.0
        self.compensation = 0.0
        self.count = 0

    def step(self, value: float | None) -> None:
        if value is not None:
            adjusted = value - self.compensation
            total = self.total + adjusted
            self.compensation = total - self.total - adjusted
            self.total = total
            self.count += 1

    def finalize(self) -> float | None:
        return self.total if self.count else None


class _CompensatedMean(_CompensatedSum):
    def finalize(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass(frozen=True)
class ChartRequest:
    name: str
    selections: tuple[tuple[str, str | None], ...]
    group_by: str = "hour"
    aggregation: str = "sum"
    sample_resolution: str = "hour"
    interval_minutes: int = 60


@dataclass
class _QueryPlan:
    request: ChartRequest
    ctes: list[str]
    output: str
    parameters: dict[str, Any]
    callbacks: dict[str, tuple[int, Any]]
    selections: tuple[tuple[str, str | None], ...]
    wide_indices: list[int]


def _build_plan(
    timezone_name: str,
    bounds: Any,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    request: ChartRequest,
) -> _QueryPlan:
    # Import here because data.py owns the chart's shared calendar conventions.
    from solar_model.storage.queries.dashboard import IMPORT_COST_TARIFF_PREFIX, PV_STRING_ENERGY_COLUMNS, PV_STRING_POWER_COLUMNS, custom_chart_split_bucket, historical_bucket_label, historical_bucket_start

    group_by, aggregation = request.group_by, request.aggregation
    sample_resolution, interval_minutes = request.sample_resolution, request.interval_minutes
    selections = request.selections
    frequencies = {"hour", "day", "week", "month", "season", "year"}
    if group_by not in frequencies:
        raise ValueError(f"Unsupported custom chart grouping: {group_by}")
    if aggregation not in {"sum", "average"}:
        raise ValueError(f"Unsupported custom chart aggregation: {aggregation}")
    if sample_resolution not in {"hour", "minute"}:
        raise ValueError(f"Unsupported sample resolution: {sample_resolution}")
    if interval_minutes < 1 or interval_minutes > 1440:
        raise ValueError("Chart interval must be between 1 and 1440 minutes")
    for _, split in selections:
        if split == group_by:
            raise ValueError("Split by must be different from Group by")
        if split is not None and split not in frequencies:
            raise ValueError(f"Unsupported custom chart split: {split}")

    zone = ZoneInfo(timezone_name)
    configured_panels = list(dict.fromkeys(array.panel_id for array in forecast_arrays))
    actual_panels = list(dict.fromkeys(actuals_to_forecast.values()))
    telemetry_columns = {
        **TELEMETRY_COLUMNS,
        **{f"power_actual_panel_{panel}": PV_STRING_POWER_COLUMNS[pv] for pv, panel in actuals_to_forecast.items()},
    }
    telemetry_metrics = {*telemetry_columns, "power_grid_import", "power_grid_export"}
    supported = {
        *ACTUAL_COLUMNS, *telemetry_metrics, "battery", "battery_flow", "import_rate", "export_rate",
        "forecast_total", "forecast_raw_total", "load_forecast",
        *(f"forecast_panel_{panel}" for panel in configured_panels),
        *(f"actual_panel_{panel}" for panel in actual_panels),
    }
    selections = tuple((metric, split) for metric, split in selections if metric in supported or metric.startswith(IMPORT_COST_TARIFF_PREFIX))
    metrics = list(dict.fromkeys(metric for metric, _ in selections if not metric.startswith(IMPORT_COST_TARIFF_PREFIX)))
    tariff_requested = any(metric.startswith(IMPORT_COST_TARIFF_PREFIX) for metric, _ in selections)
    only_telemetry = bool(metrics) and not tariff_requested and all(metric in telemetry_metrics for metric in metrics)
    if interval_minutes != 60 and not only_telemetry:
        raise ValueError("Energy variables require hourly source intervals")
    parameters: dict[str, Any] = {
        "start": ceil(bounds.start_at.timestamp()),
        "end": floor(bounds.end_at.timestamp()),
        "day_start": round(datetime.combine(bounds.start_date, time.min, zone).timestamp()),
        "day_end": round(datetime.combine(bounds.end_date + timedelta(days=1), time.min, zone).timestamp()),
        "first_day": bounds.start_date.isoformat(),
        "wall_start": bounds.start_at.replace(tzinfo=None).isoformat(),
        "wall_end": bounds.end_at.replace(tzinfo=None).isoformat(),
        "dataset": request.name,
    }

    @lru_cache(maxsize=8192)
    def local_day(stamp: int) -> str:
        return datetime.fromtimestamp(stamp, zone).date().isoformat()

    def local_hour(stamp: int) -> int:
        return datetime.fromtimestamp(stamp, zone).hour

    @lru_cache(maxsize=8192)
    def local_interval(stamp: int) -> int:
        value = datetime.fromtimestamp(stamp, zone)
        return (value.hour * 60 + value.minute) // interval_minutes

    @lru_cache(maxsize=8192)
    def forecast_hour(stamp: int, collection_day: str) -> int:
        # pandas' aware timestamp subtraction measures elapsed UTC hours.
        # Preserve this convention around DST rather than using the wall hour.
        midnight = datetime.combine(date.fromisoformat(collection_day), time.min, zone)
        return (stamp - round(midnight.timestamp())) // 3600

    @lru_cache(maxsize=8192)
    def bucket(day: str, hour: int) -> str:
        if group_by == "hour":
            minute = hour * interval_minutes
            return f"{minute // 60:02d}:{minute % 60:02d}"
        return historical_bucket_start(date.fromisoformat(day), group_by).isoformat()

    @lru_cache(maxsize=8192)
    def split_bucket(day: str, hour: int, split: str) -> tuple[int, str]:
        minute = hour * interval_minutes
        return custom_chart_split_bucket(date.fromisoformat(day), f"{minute // 60:02d}:{minute % 60:02d}", split)

    ctes = [
        "hours(hour) AS (VALUES " + ",".join(f"({index})" for index in range(0, (1439 // interval_minutes) + 1)) + ")",
        # Preserve forecast-only dates without fetching any forecast measurements.
        """collections AS (
            SELECT DISTINCT collected_at_utc FROM forecast_solar
            WHERE collected_at_utc >= :start AND collected_at_utc <= :end
        )""",
        """first_collections AS (
            SELECT chart_local_day(collected_at_utc) AS day, MIN(collected_at_utc) AS collected
            FROM collections GROUP BY day
        )""",
        """actual_dates AS (
            SELECT DISTINCT chart_local_day(hour_start_utc) AS day
            FROM sigenstor_hourly WHERE hour_start_utc >= :day_start AND hour_start_utc < :day_end
        )""",
        "available_dates AS (SELECT day FROM actual_dates UNION SELECT day FROM first_collections"
        + (" UNION SELECT target_day FROM forecast_load WHERE forecast_time >= :day_start AND forecast_time < :day_end"
           if "load_forecast" in metrics else "") + ")",
        """dates AS (
            SELECT day FROM available_dates
            UNION ALL SELECT :first_day WHERE NOT EXISTS(SELECT 1 FROM available_dates)
        )""",
    ]
    if only_telemetry:
        # Clock-time telemetry doesn't depend on forecast-only dates or energy rows.
        ctes = [ctes[0], """dates AS (
            SELECT day FROM telemetry_dates
            UNION ALL SELECT :first_day WHERE NOT EXISTS(SELECT 1 FROM telemetry_dates)
        )"""]
    hourly_fields: dict[str, str] = {}
    expressions: dict[str, str] = {}
    joins: list[str] = []

    def hourly_sum(alias: str, column: str) -> None:
        hourly_fields[alias] = f"SUM({column} / 10000.0) AS {alias}"

    for metric in metrics:
        if metric in ACTUAL_COLUMNS:
            hourly_sum(metric, ACTUAL_COLUMNS[metric])
            expressions[metric] = f"a.{metric}"
        elif metric in {"import_rate", "export_rate"}:
            # Stored rate readings are already sums of samples within each hour.
            hourly_fields[metric] = (
                f"SUM({metric} / 10000.0) / NULLIF(SUM({metric}_sample_count), 0) AS {metric}"
            )
            expressions[metric] = f"a.{metric}"
        elif metric == "battery_flow":
            hourly_sum("battery_charge", "plant_battery_charge_total_kwh_period")
            hourly_sum("battery_discharge", "plant_battery_discharge_total_kwh_period")
            expressions[metric] = "a.battery_charge - a.battery_discharge"

    requested_telemetry = [metric for metric in metrics if metric in telemetry_metrics]
    if requested_telemetry:
        table, stamp = ("sigenstor", "collected_at_utc") if sample_resolution == "minute" else ("sigenstor_hourly", "hour_start_utc")
        source_fields = []
        averaged_fields = []
        for metric in requested_telemetry:
            if metric in {"power_grid_import", "power_grid_export"}:
                if sample_resolution == "minute":
                    sign = "-" if metric == "power_grid_export" else ""
                    raw = f"MAX({sign}plant_grid_power_kw, 0) / 10000.0"
                else:
                    column = "plant_grid_import_power_kw" if metric == "power_grid_import" else "plant_grid_export_power_kw"
                    raw = f"{column} / 10000.0"
            else:
                raw = f"{telemetry_columns[metric]} / 10000.0"
            count = f"COUNT({metric})" if sample_resolution == "minute" else f"SUM(CASE WHEN {metric} IS NOT NULL THEN samples END)"
            if sample_resolution == "hour" and metric in {"power_grid_import", "power_grid_export"}:
                count = "SUM(grid_samples)"
            elif sample_resolution == "hour":
                source_fields.append(f"{telemetry_columns[metric]}_sample_count AS count_{metric}")
                count = f"SUM(count_{metric})"
            # Each day's interval contributes equally, matching the Analysis views.
            source_fields.append(f"{raw} AS {metric}")
            averaged_fields.append(f"SUM({metric}) / NULLIF({count}, 0) AS {metric}")
            expressions[metric] = f"t.{metric}"
        if sample_resolution == "hour" and any(metric in {"power_grid_import", "power_grid_export"} for metric in requested_telemetry):
            source_fields.append("grid_power_sample_count AS grid_samples")
        ctes.append(
            f"telemetry_source AS (SELECT chart_local_day({stamp}) AS day, chart_local_interval({stamp}) AS hour, "
            f"{'1' if sample_resolution == 'minute' else 'sample_count'} AS samples, " + ", ".join(source_fields) +
            f" FROM {table} WHERE {stamp} >= :start AND {stamp} <= :end)"
        )
        ctes.append(
            "telemetry_daily AS (SELECT day, hour, " + ", ".join(averaged_fields) +
            " FROM telemetry_source GROUP BY day, hour)"
        )
        ctes.append("telemetry_dates AS (SELECT DISTINCT day FROM telemetry_source)")
        joins.append("LEFT JOIN telemetry_daily t ON t.day = g.day AND t.hour = g.hour")

    if any(metric.startswith("actual_panel_") for metric in metrics):
        hourly_sum("solar", ACTUAL_COLUMNS["solar"])
        multiplicities = Counter(actuals_to_forecast.values())
        for panel in actual_panels:
            columns = [PV_STRING_ENERGY_COLUMNS[pv] for pv, mapped in actuals_to_forecast.items() if mapped == panel]
            # Sum each PV string separately so a NULL reading doesn't suppress others.
            parts = [f"SUM({column} / 10000.0)" for column in columns]
            value = " + ".join(f"COALESCE({part}, 0)" for part in parts)
            presence = " OR ".join(f"COUNT({column}) > 0" for column in columns)
            hourly_fields[f"weight_{panel}"] = f"CASE WHEN {presence} THEN {value} END AS weight_{panel}"
        weight_total = " + ".join(f"{multiplicities[panel]} * MAX(COALESCE(a.weight_{panel}, 0), 0)" for panel in actual_panels)
        for metric in metrics:
            if metric.startswith("actual_panel_"):
                panel = int(metric.removeprefix("actual_panel_"))
                expressions[metric] = (
                    f"CASE WHEN ({weight_total}) > 0 THEN a.solar * MAX(COALESCE(a.weight_{panel}, 0), 0) "
                    f"/ ({weight_total}) ELSE a.solar / {len(actuals_to_forecast)}.0 END"
                )

    battery_requested = "battery" in metrics
    if battery_requested:
        hourly_sum("battery_charge", "plant_battery_charge_total_kwh_period")
        hourly_sum("battery_discharge", "plant_battery_discharge_total_kwh_period")
        ctes.extend([
            """battery_source AS (
                SELECT chart_local_day(collected_at_utc) AS day,
                    chart_local_hour(collected_at_utc) AS hour, plant_battery_available_discharge_kwh AS value
                FROM sigenstor WHERE collected_at_utc >= :start AND collected_at_utc <= :end
            )""",
            "battery_hourly AS (SELECT day, hour, chart_mean(value / 10000.0) AS value FROM battery_source GROUP BY day, hour)",
        ])
        expressions["battery"] = (
            "CASE WHEN EXISTS(SELECT 1 FROM battery_source) THEN b.value "
            "ELSE a.battery_charge - a.battery_discharge END"
        )
        joins.append("LEFT JOIN battery_hourly b ON b.day = g.day AND b.hour = g.hour")

    if hourly_fields:
        ctes.append(
            "hourly_actuals AS (SELECT chart_local_day(hour_start_utc) AS day, "
            "chart_local_hour(hour_start_utc) AS hour, " + ", ".join(hourly_fields.values()) +
            " FROM sigenstor_hourly WHERE hour_start_utc >= :day_start AND hour_start_utc < :day_end GROUP BY day, hour)"
        )
        joins.append("LEFT JOIN hourly_actuals a ON a.day = g.day AND a.hour = g.hour")

    if "load_forecast" in metrics:
        ctes.append("""load_prediction AS (
            SELECT target_day AS day, chart_local_hour(forecast_time) AS hour,
                SUM(load_kwh / 10000.0) AS energy
            FROM forecast_load WHERE forecast_time >= :day_start AND forecast_time < :day_end
            GROUP BY target_day, hour
        )""")
        joins.append("LEFT JOIN load_prediction lp ON lp.day = g.day AND lp.hour = g.hour")
        expressions["load_forecast"] = "lp.energy"

    forecast_metrics = [metric for metric in metrics if metric.startswith("forecast_")]
    if forecast_metrics:
        points: list[str] = []
        for variant, suffix in (("adjusted", "adj"), ("raw", "raw")):
            needs_total = ("forecast_total" if variant == "adjusted" else "forecast_raw_total") in metrics
            panels = configured_panels if needs_total else [
                panel for panel in configured_panels if variant == "adjusted" and f"forecast_panel_{panel}" in metrics
            ]
            for panel in panels:
                column = f"panel_{panel}_{suffix}_watt_hours"
                points.append(
                    f"SELECT c.day, f.forecast_time AS stamp, chart_local_day(f.forecast_time) AS forecast_day, "
                    f"'{variant}' AS variant, {panel} AS panel, f.{column} / 10000.0 AS cumulative "
                    f"FROM first_collections c JOIN forecast_solar f ON f.collected_at_utc = c.collected "
                    f"WHERE f.{column} IS NOT NULL"
                )
        # Empty configured arrays produce unknown forecasts, as before.
        if points:
            ctes.extend([
                "forecast_points AS (" + " UNION ALL ".join(points) + ")",
                """forecast_differences AS (
                    SELECT *, LAG(cumulative) OVER (
                        PARTITION BY day, variant, panel, forecast_day ORDER BY stamp
                    ) AS previous FROM forecast_points
                )""",
                """forecast_energy AS (
                    SELECT day, variant, panel, chart_forecast_hour(stamp, day) AS hour,
                        MAX(cumulative - COALESCE(previous, 0), 0) / 1000.0 AS energy
                    FROM forecast_differences
                )""",
            ])
            forecast_fields: list[str] = []
            presence_fields: list[str] = []
            for metric in forecast_metrics:
                variant = "raw" if metric == "forecast_raw_total" else "adjusted"
                condition = f"variant = '{variant}'"
                if metric.startswith("forecast_panel_"):
                    condition += f" AND panel = {int(metric.removeprefix('forecast_panel_'))}"
                forecast_fields.append(f"chart_sum(CASE WHEN {condition} THEN energy END) AS {metric}")
                presence_fields.append(f"MAX(CASE WHEN {condition} THEN 1 ELSE 0 END) AS {metric}")
                expressions[metric] = f"CASE WHEN p.{metric} = 1 THEN COALESCE(f.{metric}, 0) END"
            ctes.extend([
                "forecast_presence AS (SELECT day, " + ", ".join(presence_fields) + " FROM forecast_points GROUP BY day)",
                "forecast_hourly AS (SELECT day, hour, " + ", ".join(forecast_fields) + " FROM forecast_energy WHERE hour >= 0 AND hour < 24 GROUP BY day, hour)",
            ])
            joins.extend([
                "LEFT JOIN forecast_presence p ON p.day = g.day",
                "LEFT JOIN forecast_hourly f ON f.day = g.day AND f.hour = g.hour",
            ])
        else:
            expressions.update({metric: "NULL" for metric in forecast_metrics})

    grid_sql = "SELECT day, hour FROM dates CROSS JOIN hours"
    if requested_telemetry:
        grid_sql += " UNION SELECT day, hour FROM telemetry_dates CROSS JOIN hours"
    if battery_requested:
        grid_sql += " UNION SELECT day, hour FROM battery_hourly"
    ctes.append(f"grid AS ({grid_sql})")
    def identifier(metric: str) -> str:
        return '"' + metric.replace('"', '""') + '"'

    metric_sql = "".join(f", {expressions[metric]} AS {identifier(metric)}" for metric in metrics)
    range_filter = "" if only_telemetry else (
        " WHERE g.day || 'T' || printf('%02d:00:00', g.hour) >= :wall_start"
        " AND g.day || 'T' || printf('%02d:00:00', g.hour) <= :wall_end"
    )
    ctes.append(
        "values_by_hour AS (SELECT g.day, g.hour, chart_bucket(g.day, g.hour) AS bucket" + metric_sql +
        " FROM grid g " + " ".join(joins) + range_filter + ")"
    )
    if tariff_requested:
        ctes.extend([
            """tariff_bands AS (SELECT DISTINCT import_tariff_band AS band FROM sigenstor_hourly
                WHERE hour_start_utc >= :day_start AND hour_start_utc < :day_end)""",
            """tariff_daily AS (SELECT chart_local_day(hour_start_utc) AS day,
                chart_local_hour(hour_start_utc) AS hour, import_tariff_band AS band,
                SUM(grid_import_cost_period / 10000.0) AS value FROM sigenstor_hourly
                WHERE hour_start_utc >= :day_start AND hour_start_utc < :day_end GROUP BY day, hour, band)""",
            "tariff_values AS (SELECT g.day, g.hour, chart_bucket(g.day, g.hour) AS bucket, bands.band, t.value "
            "FROM grid g CROSS JOIN tariff_bands bands LEFT JOIN tariff_daily t "
            "ON t.day = g.day AND t.hour = g.hour AND t.band = bands.band" + range_filter + ")",
        ])
    queries = ["SELECT :dataset AS dataset, bucket, NULL AS selection, NULL AS metric, NULL AS split_order, NULL AS split, NULL AS value, NULL AS packed FROM values_by_hour GROUP BY bucket"]
    tariff_queries = []
    unsplit_values = []
    wide_indices = []
    for index, (metric, split) in enumerate(selections):
        parameters[f"metric_{index}"] = metric
        is_average = aggregation == "average" or metric in {"battery", "import_rate", "export_rate", *telemetry_metrics}
        is_tariff = metric.startswith(IMPORT_COST_TARIFF_PREFIX)
        column = "value" if is_tariff else identifier(metric)
        if metric in telemetry_metrics:
            value = f"AVG({column})"
        elif metric == "load_forecast":
            value = f"chart_mean({column})" if is_average else f"chart_sum({column})"
        else:
            value = f"chart_mean({column})" if is_average else f"COALESCE(chart_sum({column}), 0)"
        if not is_tariff:
            unsplit_values.append(value)
            wide_indices.append(index)
        if split is None:
            split_fields, grouping = "NULL AS split_order, NULL AS split", "bucket"
        else:
            parameters[f"split_{index}"] = split
            split_fields = (
                f"chart_split_order(day, hour, :split_{index}) AS split_order, "
                f"chart_split_label(day, hour, :split_{index}) AS split"
            )
            grouping = "bucket, split_order, split"
        if is_tariff:
            parameters["tariff_prefix"] = IMPORT_COST_TARIFF_PREFIX
            where = ""
            if metric != IMPORT_COST_TARIFF_PREFIX + "*":
                parameters[f"tariff_{index}"] = metric.removeprefix(IMPORT_COST_TARIFF_PREFIX)
                where = f" WHERE band = :tariff_{index}"
            query = f"SELECT :dataset AS dataset, bucket, {index} AS selection, :tariff_prefix || band AS metric, {split_fields}, {value} AS value, NULL AS packed FROM tariff_values{where} GROUP BY {grouping}, band"
            tariff_queries.append(query)
            queries.append(query)
        else:
            queries.append(f"SELECT :dataset AS dataset, bucket, {index} AS selection, :metric_{index} AS metric, {split_fields}, {value} AS value, NULL AS packed FROM values_by_hour GROUP BY {grouping}")
    wide_unsplit = bool(selections) and all(split is None for _, split in selections)
    output_query = (
        "SELECT :dataset AS dataset, bucket, NULL AS selection, NULL AS metric, NULL AS split_order, NULL AS split, NULL AS value, chart_pack(" + ", ".join(unsplit_values) + ") AS packed FROM values_by_hour GROUP BY bucket" +
        (" UNION ALL " + " UNION ALL ".join(tariff_queries) if tariff_queries else "")
        if wide_unsplit else " UNION ALL ".join(queries)
    )
    callbacks = {
        "chart_local_day": (1, local_day), "chart_local_hour": (1, local_hour),
        "chart_local_interval": (1, local_interval), "chart_forecast_hour": (2, forecast_hour),
        "chart_bucket": (2, bucket),
        "chart_split_order": (3, lambda day, hour, split: split_bucket(day, hour, split)[0]),
        "chart_split_label": (3, lambda day, hour, split: split_bucket(day, hour, split)[1]),
    }
    return _QueryPlan(request, ctes, output_query, parameters, callbacks, selections, wide_indices)


def _format_dataset(plan: _QueryPlan, rows: list[Any]) -> dict[str, Any]:
    from solar_model.storage.queries.dashboard import historical_bucket_label

    buckets = sorted({row.bucket for row in rows if row.metric is None})
    if not buckets:
        return {"periods": [], "series": [], "empty_message": "No data in the selected Analysis range"}
    group_by = plan.request.group_by
    periods = buckets if group_by == "hour" else [historical_bucket_label(date.fromisoformat(value), group_by) for value in buckets]
    values: dict[tuple[int, str, str | None], dict[str, Any]] = {}
    split_orders: dict[tuple[int, str, str | None], int] = {}
    for row in rows:
        if row.packed is not None:
            for index, value in zip(plan.wide_indices, json.loads(row.packed)):
                key = (index, plan.selections[index][0], None)
                values.setdefault(key, {})[row.bucket] = value
                split_orders[key] = 0
        elif row.metric is not None:
            key = (row.selection, row.metric, row.split)
            values.setdefault(key, {})[row.bucket] = row.value
            split_orders[key] = row.split_order or 0
    series = []
    for index, (metric, split) in enumerate(plan.selections):
        keys = sorted(
            (key for key in values if key[0] == index),
            key=lambda key: (key[1], split_orders[key]),
        )
        for key in keys:
            series.append({"metric": key[1], "split": key[2], "data": [values[key].get(value) for value in buckets]})
    return {"periods": periods, "series": series}


def _compose_batch(plans: list[_QueryPlan]) -> tuple[str, dict[str, Any], dict[str, tuple[int, Any]]]:
    """Intern common CTEs and project shared sources once for the entire batch."""
    shared_parameters = {"start", "end", "day_start", "day_end", "first_day", "wall_start", "wall_end", "tariff_prefix"}
    parameters: dict[str, Any] = {}
    callbacks: dict[str, tuple[int, Any]] = {}
    source_columns = {"sigenstor_hourly": set(), "sigenstor": set(), "forecast_solar": set()}
    models = {"sigenstor_hourly": SigenStorHourlySample, "sigenstor": SigenStorModbusSample, "forecast_solar": ForecastSolarSample}
    for plan in plans:
        for cte in plan.ctes:
            words = set(re.findall(r"\b\w+\b", cte))
            for table, model in models.items():
                if table in words:
                    source_columns[table].update(column.name for column in model.__table__.columns if column.name in words)

    ctes: list[str] = []
    sources = {"sigenstor_hourly": "batch_hourly", "sigenstor": "batch_minutes", "forecast_solar": "batch_forecasts"}
    for table, stamp, where in (
        ("sigenstor_hourly", "hour_start_utc", "hour_start_utc >= :day_start AND hour_start_utc < :day_end"),
        ("sigenstor", "collected_at_utc", "collected_at_utc >= :start AND collected_at_utc <= :end"),
    ):
        if source_columns[table]:
            columns = ", ".join(sorted(source_columns[table] | {stamp}))
            ctes.append(f"{sources[table]} AS MATERIALIZED (SELECT {columns} FROM {table} WHERE {where})")
    if any("collections AS" in cte for plan in plans for cte in plan.ctes):
        ctes.extend([
            """batch_collections AS MATERIALIZED (
                SELECT DISTINCT collected_at_utc FROM forecast_solar
                WHERE collected_at_utc >= :start AND collected_at_utc <= :end
            )""",
            """batch_first_collections AS MATERIALIZED (
                SELECT chart_local_day(collected_at_utc) AS day, MIN(collected_at_utc) AS collected
                FROM batch_collections GROUP BY day
            )""",
        ])
    if any(cte.startswith("forecast_points AS") for plan in plans for cte in plan.ctes):
        columns = ", ".join(f"f.{column}" for column in sorted(source_columns["forecast_solar"] | {"collected_at_utc", "forecast_time"}))
        ctes.append(
            f"batch_forecasts AS MATERIALIZED (SELECT {columns} FROM forecast_solar f "
            "JOIN batch_first_collections c ON f.collected_at_utc = c.collected)"
        )

    interned: dict[str, str] = {}
    outputs = []
    contextual_functions = {"chart_bucket", "chart_local_interval", "chart_split_order", "chart_split_label"}
    for plan_index, plan in enumerate(plans):
        prefix = f"r{plan_index}_"
        for key, value in plan.parameters.items():
            parameters[key if key in shared_parameters else prefix + key] = value
        for key, value in plan.callbacks.items():
            callbacks.setdefault(prefix + key if key in contextual_functions else key, value)

        def namespace(sql: str) -> str:
            sql = re.sub(r":([A-Za-z_]\w*)", lambda match: ":" + (match[1] if match[1] in shared_parameters else prefix + match[1]), sql)
            for name in contextual_functions:
                sql = re.sub(r"\b" + name + r"\b", prefix + name, sql)
            return sql

        definitions: dict[str, str] = {}
        declared_columns: dict[str, str] = {}
        for cte in plan.ctes:
            match = re.fullmatch(r"\s*(\w+)\s*(\([^()]*\))?\s+AS\s*\((.*)\)\s*", cte, flags=re.DOTALL)
            if match is None:
                raise ValueError("Invalid chart query component")
            declared_columns[match[1]] = match[2] or ""
            definitions[match[1]] = namespace(match[3])
        references = re.compile(r"\b(" + "|".join(sorted(definitions, key=len, reverse=True)) + r")\b")
        resolved: dict[str, str] = {}

        def resolve(name: str) -> str:
            if name in resolved:
                return resolved[name]
            body = definitions[name]
            if name == "collections":
                body = "SELECT collected_at_utc FROM batch_collections"
            elif name == "first_collections":
                body = "SELECT day, collected FROM batch_first_collections"
            else:
                body = references.sub(lambda match: resolve(match[1]), body)
                for table, source in sources.items():
                    body = re.sub(r"\b" + table + r"\b", source, body)
            canonical = re.sub(r"\s+", " ", body).strip()
            if canonical not in interned:
                shared_name = f"component_{len(interned)}"
                interned[canonical] = shared_name
                ctes.append(f"{shared_name}{declared_columns[name]} AS ({body})")
            resolved[name] = interned[canonical]
            return resolved[name]

        outputs.append(references.sub(lambda match: resolve(match[1]), namespace(plan.output)))
    return "WITH " + ",\n".join(ctes) + "\n" + " UNION ALL ".join(outputs), parameters, callbacks


def aggregate_chart_data_batch(
    database_path: str,
    timezone_name: str,
    bounds: Any,
    forecast_arrays: tuple[SolarArrayConfig, ...],
    actuals_to_forecast: dict[str, int],
    requests: tuple[ChartRequest, ...],
    timings: Any,
) -> dict[str, dict[str, Any]]:
    """Run all requested datasets in one statement and one consistent snapshot."""
    names = [request.name for request in requests]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Chart request names must be nonempty and unique")
    datasets = {name: {"periods": [], "series": []} for name in names}
    started_at = perf_counter()
    plans = [_build_plan(timezone_name, bounds, forecast_arrays, actuals_to_forecast, request) for request in requests if request.selections]
    if not plans:
        return datasets
    sql, parameters, callbacks = _compose_batch(plans)
    timings.record("SQL: prepare batch and shared sources", started_at)
    engine = create_engine_for_database(database_path)
    try:
        with engine.connect() as connection:
            sqlite = connection.connection.driver_connection
            sqlite.create_aggregate("chart_sum", 1, _CompensatedSum)
            sqlite.create_aggregate("chart_mean", 1, _CompensatedMean)
            # Python's float encoding preserves all precision; SQLite's default
            # JSON number rendering can truncate significant digits.
            sqlite.create_function("chart_pack", -1, lambda *values: json.dumps(values, separators=(",", ":")), deterministic=True)
            for name, (arity, callback) in callbacks.items():
                sqlite.create_function(name, arity, callback, deterministic=True)
            started_at = perf_counter()
            result = connection.exec_driver_sql(sql, parameters)
            timings.record("SQL: execute unified aggregation", started_at)
            started_at = perf_counter()
            rows = result.fetchall()
            timings.record("SQL: fetch batch results", started_at)
            timings.rows["SQL: fetch batch results"] = len(rows)
    finally:
        engine.dispose()
    started_at = perf_counter()
    grouped: dict[str, list[Any]] = {plan.request.name: [] for plan in plans}
    for row in rows:
        grouped[row.dataset].append(row)
    for plan in plans:
        datasets[plan.request.name] = _format_dataset(plan, grouped[plan.request.name])
    timings.record("Chart: format batch datasets", started_at)
    return datasets
