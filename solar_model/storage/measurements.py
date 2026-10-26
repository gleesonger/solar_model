from __future__ import annotations
import math
import re
from datetime import date, datetime, time
from typing import Any, cast
from zoneinfo import ZoneInfo
from sqlalchemy import Integer, case, cast as sql_cast, func, inspect, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from solar_model.config import TariffsConfig
from solar_model.integrations.sigenstor.datasheets import SIGENSTOR_MODEL_SPECIFICATIONS
from solar_model.tariffs import economic_period_values
from solar_model.logging_setup import LOGGER, heavy_work

from solar_model.storage.schema import (
    column_name,
    SigenStorModbusSample,
    SigenStorDevice,
    DatabaseMetadata,
    CUMULATIVE_COLUMNS,
    PV_ARRAY_IDS,
    PV_ARRAY_POWER_COLUMNS,
    HOURLY_MEASUREMENT_COLUMNS,
    HOURLY_GRID_POWER_COLUMNS,
    HOURLY_VALID_COUNT_COLUMNS,
    HOURLY_ROLLUP_MARKER,
    SigenStorHourlySample,
)

def house_load_energy(previous_kw: float | None, current_kw: float | None, elapsed_seconds: float) -> float | None:
    """Estimated household kWh; do not integrate across missing/long intervals."""
    if (previous_kw is None or current_kw is None
            or not math.isfinite(previous_kw) or not math.isfinite(current_kw)
            or not 0 < elapsed_seconds <= 120):
        return None
    return (previous_kw + current_kw) / 2 * elapsed_seconds / 3600


class MeasurementStorage:
    """Atomic telemetry, device history and derived hourly rollups."""

    def load_previous_cumulatives(self) -> dict[str, float]:
        with self.sessions() as session:
            row = session.scalar(select(SigenStorModbusSample).order_by(SigenStorModbusSample.collected_at_utc.desc()).limit(1))
        previous = {
            column: getattr(row, column)
            for column in CUMULATIVE_COLUMNS
            if row is not None and getattr(row, column) is not None
        }
        if row is not None:
            previous["_collected_at_utc"] = row.collected_at_utc
            previous.update({column: getattr(row, column) for column in PV_ARRAY_POWER_COLUMNS})
            previous["plant_load_power_kw"] = row.plant_load_power_kw
        return previous

    def save_modbus_sample(
        self,
        values: dict[str, tuple[float, str, bool]],
        previous: dict[str, float],
        collected: tuple[str, str],
        tariffs: TariffsConfig,
    ) -> None:
        row: dict[str, Any] = {"collected_at_utc": collected[0]}
        for metric, (value, unit, cumulative) in values.items():
            name = column_name(metric, unit)
            if name not in SigenStorModbusSample.__table__.c:
                raise ValueError(f"Modbus metric {metric!r} is not declared in ModbusSampleWide")
            row[name] = value
            if cumulative:
                row[f"{name}_period"] = value - previous[name] if name in previous and value >= previous[name] else None
        self._add_pv_array_measurements(row, previous)
        previous_stamp = previous.get("_collected_at_utc")
        row["house_load_kwh_period"] = house_load_energy(
            previous.get("plant_load_power_kw"), row.get("plant_load_power_kw"),
            datetime.fromisoformat(collected[0]).timestamp()
            - datetime.fromisoformat(cast(str, previous_stamp)).timestamp()
            if previous_stamp is not None else 0,
        )
        row.update(economic_period_values(
            row.get("plant_grid_import_total_kwh_period"),
            row.get("plant_grid_export_total_kwh_period"),
            row.get("house_load_kwh_period"),
            tariffs,
            datetime.fromisoformat(collected[1]),
        ))
        with self.sessions.begin() as session:
            session.add(SigenStorModbusSample(**row))
            self._add_hourly_sample(session, row)
        previous.update({column_name(metric, unit): value for metric, (value, unit, _) in values.items()})
        previous["_collected_at_utc"] = collected[0]
        previous.update({column: row.get(column) for column in PV_ARRAY_POWER_COLUMNS})
        previous["plant_load_power_kw"] = row.get("plant_load_power_kw")

    @staticmethod
    def _add_pv_array_measurements(row: dict[str, Any], previous: dict[str, float]) -> None:
        """Derive PV-string power and interval energy once at collection time."""
        timestamp = datetime.fromisoformat(cast(str, row["collected_at_utc"]))
        previous_timestamp_text = previous.get("_collected_at_utc")
        previous_timestamp = (
            datetime.fromisoformat(cast(str, previous_timestamp_text))
            if previous_timestamp_text is not None
            else None
        )
        elapsed_hours = (
            (timestamp - previous_timestamp).total_seconds() / 3_600
            if previous_timestamp is not None
            else None
        )
        for panel_id in PV_ARRAY_IDS:
            voltage = row.get(f"inverter_pv{panel_id}_voltage_volts")
            current = row.get(f"inverter_pv{panel_id}_current_amps")
            power_column = f"inverter_pv{panel_id}_power_kw"
            energy_column = f"inverter_pv{panel_id}_energy_kwh_period"
            power = (
                max(cast(float, voltage) * cast(float, current), 0.0) / 1_000
                if voltage is not None and current is not None
                else None
            )
            row[power_column] = power
            previous_power = previous.get(power_column)
            row[energy_column] = (
                (cast(float, previous_power) + power) / 2 * elapsed_hours
                if power is not None
                and previous_power is not None
                and elapsed_hours is not None
                and elapsed_hours > 0
                else None
            )

    @staticmethod
    def _add_hourly_sample(session, row: dict[str, Any]) -> None:
        """Atomically add one raw sample to its hourly/tariff aggregate."""
        timestamp = datetime.fromisoformat(cast(str, row["collected_at_utc"]))
        hour_start_utc = int(timestamp.timestamp()) // 3600 * 3600
        hourly_table = SigenStorHourlySample.__table__
        hourly_values: dict[str, Any] = {
            "hour_start_utc": hour_start_utc,
            "import_tariff_band": row.get("import_tariff_band") or "Unknown",
            "sample_count": 1,
        }
        hourly_values.update({name: row.get(name) for name in HOURLY_MEASUREMENT_COLUMNS})
        grid_power = row.get("plant_grid_power_kw")
        hourly_values.update({
            "grid_power_sample_count": int(grid_power is not None),
            "plant_grid_import_power_kw": max(grid_power, 0.0) if grid_power is not None else None,
            "plant_grid_export_power_kw": max(-grid_power, 0.0) if grid_power is not None else None,
        })
        hourly_values.update({count: int(row.get(name) is not None) for name, count in HOURLY_VALID_COUNT_COLUMNS.items()})

        statement = sqlite_insert(hourly_table).values(hourly_values)
        update_values: dict[str, Any] = {
            "sample_count": hourly_table.c.sample_count + statement.excluded.sample_count,
            "grid_power_sample_count": func.coalesce(hourly_table.c.grid_power_sample_count, 0) + statement.excluded.grid_power_sample_count,
        }
        update_values.update({count: func.coalesce(hourly_table.c[count], 0) + statement.excluded[count] for count in HOURLY_VALID_COUNT_COLUMNS.values()})
        for name in (*HOURLY_MEASUREMENT_COLUMNS, *HOURLY_GRID_POWER_COLUMNS):
            current = hourly_table.c[name]
            incoming = statement.excluded[name]
            if name in CUMULATIVE_COLUMNS:
                update_values[name] = case(
                    (current.is_(None), incoming),
                    (incoming.is_(None), current),
                    else_=func.max(current, incoming),
                )
            else:
                update_values[name] = case(
                    (current.is_(None), incoming),
                    (incoming.is_(None), current),
                    else_=current + incoming,
                )
        session.execute(statement.on_conflict_do_update(
            index_elements=(hourly_table.c.hour_start_utc, hourly_table.c.import_tariff_band),
            set_=update_values,
        ))
    @heavy_work("hourly rollup historical backfill")

    def backfill_hourly_rollup(self) -> None:
        """Populate a newly-created hourly table from all raw minute samples."""
        with self.engine.begin() as connection:
            result = self._insert_hourly_rollup(connection)
        LOGGER.info("Hourly rollup historical backfill inserted %s rows", result.rowcount)

    def initialize_hourly_rollup(self) -> None:
        """Publish initial rollup readiness only with a complete raw-data rebuild."""
        marker = HOURLY_ROLLUP_MARKER
        with self.engine.begin() as connection:
            # Serialize the completion check and rebuild with collector writes.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            table_missing = not inspect(connection).has_table(SigenStorHourlySample.__table__.name)
            if table_missing:
                SigenStorHourlySample.__table__.create(connection)
            initialized = connection.execute(
                select(DatabaseMetadata.value).where(DatabaseMetadata.key == marker)
            ).scalar_one_or_none()
            if initialized == "1" and not table_missing:
                return
            # Older openers could leave a present but incomplete table. Rebuild
            # once rather than trusting existence or ignoring stale groups.
            LOGGER.info("Initializing/recovering hourly rollup from raw history")
            connection.execute(SigenStorHourlySample.__table__.delete())
            result = self._insert_hourly_rollup(connection)
            statement = sqlite_insert(DatabaseMetadata.__table__).values(key=marker, value="1")
            connection.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"value": "1"}))
        LOGGER.info("Hourly rollup initialization completed (%s groups)", result.rowcount)
    @heavy_work("hourly rollup range rebuild")

    def rebuild_hourly_rollup(self, start: date, end: date, timezone: ZoneInfo, *, connection: Connection | None = None) -> None:
        """Replace hourly aggregates for an inclusive local-date range.

        Tariff backdating changes minute-level costs and rates.  Rebuilding
        only the affected UTC hours keeps the dashboard's rollup source in
        sync without touching unrelated historical data.
        """
        if end < start:
            raise ValueError("end date must not be before start date")
        start_epoch = int(datetime.combine(start, time.min, timezone).timestamp())
        end_epoch = int(datetime.combine(
            date.fromordinal(end.toordinal() + 1), time.min, timezone,
        ).timestamp())
        # A local midnight can fall within a UTC bucket. Replace every whole
        # overlapping bucket, including its observations outside the local day.
        start_epoch = start_epoch // 3600 * 3600
        end_epoch = -(-end_epoch // 3600) * 3600
        if connection is None:
            with self.engine.begin() as owned_connection:
                self._rebuild_hourly_rollup(owned_connection, start_epoch, end_epoch)
        else:
            self._rebuild_hourly_rollup(connection, start_epoch, end_epoch)
        LOGGER.info("Hourly rollup rebuilt from %s through %s", start, end)

    def _rebuild_hourly_rollup(self, connection: Connection, start_epoch: int, end_epoch: int) -> None:
        target = SigenStorHourlySample.__table__
        connection.execute(target.delete().where(
            target.c.hour_start_utc >= start_epoch,
            target.c.hour_start_utc < end_epoch,
        ))
        self._insert_hourly_rollup(connection, start_epoch, end_epoch)

    @staticmethod
    def _insert_hourly_rollup(connection, start_epoch: int | None = None, end_epoch: int | None = None):
        """Insert hourly aggregates from source rows in an optional UTC range."""
        source = SigenStorModbusSample.__table__
        target = SigenStorHourlySample.__table__
        hour_start = sql_cast(source.c.collected_at_utc / 3600, Integer) * 3600
        tariff_band = func.coalesce(source.c.import_tariff_band, literal("Unknown"))
        aggregate_values = [
            (
                func.max(source.c[name])
                if name in CUMULATIVE_COLUMNS
                else func.sum(source.c[name])
            ).label(name)
            for name in HOURLY_MEASUREMENT_COLUMNS
        ]
        source_query = select(
            hour_start.label("hour_start_utc"),
            tariff_band.label("import_tariff_band"),
            func.count().label("sample_count"),
            func.count(source.c.plant_grid_power_kw).label("grid_power_sample_count"),
            func.sum(func.max(source.c.plant_grid_power_kw, 0)).label("plant_grid_import_power_kw"),
            func.sum(func.max(-source.c.plant_grid_power_kw, 0)).label("plant_grid_export_power_kw"),
            *(func.count(source.c[name]).label(count) for name, count in HOURLY_VALID_COUNT_COLUMNS.items()),
            *aggregate_values,
        )
        if start_epoch is not None:
            source_query = source_query.where(source.c.collected_at_utc >= start_epoch)
        if end_epoch is not None:
            source_query = source_query.where(source.c.collected_at_utc < end_epoch)
        source_query = source_query.group_by(hour_start, tariff_band)
        statement = sqlite_insert(target).from_select(
            ("hour_start_utc", "import_tariff_band", "sample_count", "grid_power_sample_count",
             *HOURLY_GRID_POWER_COLUMNS, *HOURLY_VALID_COUNT_COLUMNS.values(), *HOURLY_MEASUREMENT_COLUMNS),
            source_query,
        ).prefix_with("OR IGNORE")
        return connection.execute(statement)

    def save_device_info(
        self,
        values: dict[str, tuple[float | str, str, bool]],
        collected: tuple[str, str],
        variable_names: dict[str, str] | None = None,
    ) -> int:
        values_to_save = values.copy()
        model_reading = values.get("Inverter model")
        if model_reading is not None and isinstance(model_reading[0], str):
            maximum_pv_power = maximum_pv_power_for_model(model_reading[0])
            if maximum_pv_power is not None:
                values_to_save["Maximum PV input power"] = (
                    maximum_pv_power,
                    "kW",
                    False,
                )
        valid_values = {
            variable: (value_text, unit)
            for variable, (value, unit, _) in values_to_save.items()
            if (value_text := device_value_text(value)) is not None
        }
        if not valid_values:
            return 0

        changed = 0
        with self.sessions.begin() as session:
            previous_rows = session.scalars(
                select(SigenStorDevice).order_by(SigenStorDevice.id.desc())
            ).all()
            latest_by_variable: dict[str, SigenStorDevice] = {}
            for row in previous_rows:
                latest_by_variable.setdefault(row.variable, row)

            for variable, (value, unit) in valid_values.items():
                previous = latest_by_variable.get(variable)
                if previous is not None and previous.value == value and previous.unit == unit:
                    continue
                session.add(SigenStorDevice(
                    collected_at_utc=collected[0],
                    collected_at_local=collected[1],
                    variable_name=(variable_names or {}).get(variable, _device_variable_name(variable)),
                    variable=variable,
                    value=value,
                    unit=unit,
                ))
                changed += 1
        return changed

def device_value_text(value: float | str) -> str | None:
    if isinstance(value, str):
        cleaned = value.replace("\x00", "").strip()
        return cleaned if cleaned and cleaned != "0" else None
    if not math.isfinite(value) or value == 0:
        return None
    return format(value, ".15g")

def _device_variable_name(variable: str) -> str:
    """Create a stable lower-case name for legacy/derived device values."""
    return re.sub(r"[^a-z0-9]+", "_", variable.casefold()).strip("_")

def maximum_pv_power_for_model(model: str) -> float | None:
    return next(
        (
            maximum_power
            for modbus_name, datasheet_name, maximum_power in SIGENSTOR_MODEL_SPECIFICATIONS
            if model == modbus_name or model == datasheet_name
        ),
        None,
    )
