from __future__ import annotations

from functools import cache
import logging
import math
import re
from datetime import date, datetime, time
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Column, Index, Integer, String, Table, Text, case, cast as sql_cast, create_engine, event, func, inspect, literal, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

from config import SolarArrayConfig, TariffsConfig
from datasheets import SIGENSTOR_MODEL_SPECIFICATIONS
from economics import economic_period_values
from common import LOGGER, heavy_work


def column_name(metric: str, unit: str) -> str:
    suffix = {"%": "percent", "kWh": "kwh", "kW": "kw", "V": "volts", "A": "amps", "Hz": "hz"}.get(unit, unit)
    metric = re.sub(r"[^a-zA-Z0-9_]", "_", metric).lower()
    return metric if metric.endswith(f"_{suffix}") else f"{metric}_{suffix}"


class ScaledInteger(TypeDecorator[float]):
    """Store measurements as integers while presenting their original units."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: float | None, dialect) -> int | None:
        return None if value is None else round(value * 10_000)

    def process_result_value(self, value: int | None, dialect) -> float | None:
        return None if value is None else value / 10_000


class EpochSeconds(TypeDecorator[str]):
    """Store timestamps as UTC epoch seconds while keeping the existing API."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: str | datetime | int | None, dialect) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo("Europe/Dublin"))
        return round(timestamp.timestamp())

    def process_result_value(self, value: int | None, dialect) -> str | None:
        return None if value is None else datetime.fromtimestamp(value, ZoneInfo("UTC")).isoformat()


# Existing model declarations use ``Float`` for every measurement. Keeping this
# alias preserves their readable field declarations while changing storage.
Float = ScaledInteger


class Base(DeclarativeBase):
    pass


class SigenStorModbusSample(Base):
    __tablename__ = "sigenstor"

    collected_at_utc: Mapped[str] = mapped_column(EpochSeconds, primary_key=True)

    @hybrid_property
    def collected_at_local(self) -> str:
        return datetime.fromisoformat(self.collected_at_utc).astimezone().isoformat()

    @collected_at_local.expression
    @classmethod
    def collected_at_local(cls):
        return func.strftime("%Y-%m-%dT%H:%M:%S", cls.collected_at_utc, "unixepoch", "localtime")

    plant_grid_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_pv_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_battery_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_battery_soc_percent: Mapped[float | None] = mapped_column(Float)
    plant_battery_available_discharge_kwh: Mapped[float | None] = mapped_column(Float)
    plant_pv_daily_kwh: Mapped[float | None] = mapped_column(Float)
    plant_pv_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_pv_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_pv_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_load_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_load_daily_kwh: Mapped[float | None] = mapped_column(Float)
    plant_load_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_load_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_load_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_grid_import_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_grid_import_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_grid_export_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_grid_export_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    import_tariff_band: Mapped[str | None] = mapped_column(Text)
    import_rate: Mapped[float | None] = mapped_column(Float)
    export_rate: Mapped[float | None] = mapped_column(Float)
    grid_import_cost_period: Mapped[float | None] = mapped_column(Float)
    grid_export_revenue_period: Mapped[float | None] = mapped_column(Float)
    net_cost_period: Mapped[float | None] = mapped_column(Float)
    no_solar_battery_import_cost_period: Mapped[float | None] = mapped_column(Float)
    plant_battery_charge_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_battery_charge_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_battery_discharge_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_battery_discharge_total_kwh_period: Mapped[float | None] = mapped_column(Float)

    inverter_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_battery_avg_cell_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_battery_charge_daily_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_charge_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_battery_discharge_daily_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_discharge_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_energy_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_energy_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_energy_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_energy_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_grid_frequency_hz: Mapped[float | None] = mapped_column(Float)
    inverter_phase_a_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_phase_b_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_phase_c_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_phase_a_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_phase_b_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_phase_c_current_amps: Mapped[float | None] = mapped_column(Float)


class SigenStorDevice(Base):
    __tablename__ = "sigenstor_devices"
    __table_args__ = (Index("idx_sigenstor_devices_variable_id", "variable", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collected_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    collected_at_local: Mapped[str] = mapped_column(String, nullable=False)
    variable_name: Mapped[str | None] = mapped_column(String, nullable=True)
    variable: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)


class DatabaseMetadata(Base):
    __tablename__ = "database_metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class ForecastSolarSample(Base):
    __tablename__ = "forecast_solar"
    __table_args__ = (Index("idx_forecast_time", "forecast_time"),)

    collected_at_utc: Mapped[str] = mapped_column(EpochSeconds, primary_key=True)
    forecast_time: Mapped[str] = mapped_column(EpochSeconds, primary_key=True)

    @hybrid_property
    def collected_at_local(self) -> str:
        return datetime.fromisoformat(self.collected_at_utc).astimezone().isoformat()

    @collected_at_local.expression
    @classmethod
    def collected_at_local(cls):
        return func.strftime("%Y-%m-%dT%H:%M:%S", cls.collected_at_utc, "unixepoch", "localtime")

    # ``raw`` is the Forecast.Solar response, exactly as collected.  The
    # adjustment model owns the corresponding ``adj`` values; keeping both on
    # the same snapshot row makes comparisons reproducible.
    panel_1_raw_watts: Mapped[float | None] = mapped_column(Float)
    panel_1_raw_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_1_raw_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_1_adj_watts: Mapped[float | None] = mapped_column(Float)
    panel_1_adj_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_1_adj_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_2_raw_watts: Mapped[float | None] = mapped_column(Float)
    panel_2_raw_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_2_raw_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_2_adj_watts: Mapped[float | None] = mapped_column(Float)
    panel_2_adj_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_2_adj_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_3_raw_watts: Mapped[float | None] = mapped_column(Float)
    panel_3_raw_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_3_raw_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_3_adj_watts: Mapped[float | None] = mapped_column(Float)
    panel_3_adj_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_3_adj_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_4_raw_watts: Mapped[float | None] = mapped_column(Float)
    panel_4_raw_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_4_raw_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_4_adj_watts: Mapped[float | None] = mapped_column(Float)
    panel_4_adj_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_4_adj_watt_hours_day: Mapped[float | None] = mapped_column(Float)


CUMULATIVE_COLUMNS = (
    "plant_pv_daily_kwh", "plant_pv_total_kwh", "plant_load_daily_kwh", "plant_load_total_kwh",
    "plant_grid_import_total_kwh", "plant_grid_export_total_kwh", "plant_battery_charge_total_kwh",
    "plant_battery_discharge_total_kwh", "inverter_battery_charge_daily_kwh",
    "inverter_battery_discharge_daily_kwh",
)
PV_ARRAY_IDS = (1, 2, 3, 4)
PV_ARRAY_POWER_COLUMNS = tuple(f"inverter_pv{panel_id}_power_kw" for panel_id in PV_ARRAY_IDS)
PV_ARRAY_ENERGY_COLUMNS = tuple(
    f"inverter_pv{panel_id}_energy_kwh_period" for panel_id in PV_ARRAY_IDS
)
PV_ARRAY_COLUMNS = PV_ARRAY_POWER_COLUMNS + PV_ARRAY_ENERGY_COLUMNS

# The hourly table deliberately has the same measurement names as ``sigenstor``.
# Its numeric values are aggregates: sums for readings and period values, and
# maximums for cumulative meters. ``sample_count`` is used to recover averages.
HOURLY_MEASUREMENT_COLUMNS = tuple(
    column.name
    for column in SigenStorModbusSample.__table__.columns
    if column.name not in {"collected_at_utc", "import_tariff_band"}
)


class SigenStorHourlySample(Base):
    __table__ = Table(
        "sigenstor_hourly",
        Base.metadata,
        Column("hour_start_utc", EpochSeconds, primary_key=True),
        # A non-null sentinel permits an hourly row even if a source sample is
        # missing its tariff band, and keeps the composite key unambiguous.
        Column("import_tariff_band", Text, primary_key=True, nullable=False),
        Column("sample_count", Integer, nullable=False),
        *(
            Column(column.name, column.type, nullable=True)
            for column in SigenStorModbusSample.__table__.columns
            if column.name not in {"collected_at_utc", "import_tariff_band"}
        ),
    )

class SolarDatabase:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.sessions = sessionmaker(engine)

    def close(self) -> None:
        self.engine.dispose()

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
        return previous

    def save_forecast(
        self,
        array: SolarArrayConfig,
        payload: dict,
        collected: tuple[str, str],
    ) -> int:
        result = payload.get("result", {})
        watts = result.get("watts", {})
        watt_hours = result.get("watt_hours", {})
        daily = result.get("watt_hours_day", {})
        prefix = f"panel_{array.panel_id}"
        fields = (f"{prefix}_raw_watts", f"{prefix}_raw_watt_hours", f"{prefix}_raw_watt_hours_day")
        if any(field not in ForecastSolarSample.__table__.c for field in fields):
            raise ValueError(
                f"forecast panel ID {array.panel_id!r} is not declared in ForecastSolarSample"
            )

        rows = 0
        with self.sessions.begin() as session:
            for forecast_time in sorted(set(watts) | set(watt_hours)):
                values: dict[str, Any] = {
                    "collected_at_utc": collected[0],
                    "forecast_time": forecast_time,
                    fields[0]: watts.get(forecast_time),
                    fields[1]: watt_hours.get(forecast_time),
                    fields[2]: daily.get(forecast_time[:10]),
                }
                row = session.scalar(select(ForecastSolarSample).where(
                    ForecastSolarSample.collected_at_utc == collected[0],
                    ForecastSolarSample.forecast_time == forecast_time,
                ))
                if row is None:
                    session.add(ForecastSolarSample(**values))
                else:
                    for name, value in values.items():
                        setattr(row, name, value)
                rows += 1
        return rows

    @staticmethod
    def _pending_adjusted_condition():
        """Return rows with at least one raw panel still lacking adjustment."""
        return or_(*(
            getattr(ForecastSolarSample, f"panel_{panel_id}_raw_watts").is_not(None)
            & getattr(ForecastSolarSample, f"panel_{panel_id}_adj_watts").is_(None)
            for panel_id in PV_ARRAY_IDS
        ))

    def pending_adjusted_forecasts(
        self,
        limit: int | None = None,
        collected_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw forecast rows which have not yet been adjusted.

        The returned dictionaries intentionally include all raw and adjusted
        columns.  This is the persistence boundary used by the adjustment
        model and its resumable backfill: a row stops being pending once every
        panel with a raw power forecast has an adjusted power forecast.
        """
        statement = select(ForecastSolarSample).where(self._pending_adjusted_condition())
        if collected_at is not None:
            statement = statement.where(ForecastSolarSample.collected_at_utc == collected_at)
        statement = statement.order_by(
            ForecastSolarSample.collected_at_utc,
            ForecastSolarSample.forecast_time,
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.sessions() as session:
            rows = session.scalars(statement).all()
        columns = tuple(ForecastSolarSample.__table__.columns.keys())
        return [{column: getattr(row, column) for column in columns} for row in rows]

    def pending_adjusted_forecast_snapshots(self, limit: int) -> list[str]:
        """Return a bounded, oldest-first set of incomplete snapshots."""
        if limit < 1:
            raise ValueError("limit must be at least one snapshot")
        statement = (
            select(ForecastSolarSample.collected_at_utc)
            .where(self._pending_adjusted_condition())
            .distinct()
            .order_by(ForecastSolarSample.collected_at_utc)
            .limit(limit)
        )
        with self.sessions() as session:
            return list(session.scalars(statement))

    def save_adjusted_forecast(
        self,
        collected_at: str,
        forecast_time: str,
        values: dict[str, float | None],
    ) -> None:
        """Persist adjusted values for one existing forecast snapshot row.

        ``values`` may contain only ``panel_n_adj_*`` fields.  The explicit
        restriction prevents an adjustment/backfill job from ever changing the
        immutable raw API response.
        """
        self.save_adjusted_forecasts(collected_at, {forecast_time: values})

    def save_adjusted_forecasts(
        self,
        collected_at: str,
        values_by_forecast_time: dict[str, dict[str, float | None]],
    ) -> None:
        """Atomically save all adjusted rows belonging to one snapshot."""
        if not values_by_forecast_time:
            return
        allowed = {
            column.name for column in ForecastSolarSample.__table__.columns
            if "_adj_" in column.name
        }
        for values in values_by_forecast_time.values():
            unexpected = set(values) - allowed
            if unexpected:
                raise ValueError(f"only adjusted forecast fields may be saved: {sorted(unexpected)!r}")
        with self.sessions.begin() as session:
            for forecast_time, values in values_by_forecast_time.items():
                row = session.scalar(select(ForecastSolarSample).where(
                    ForecastSolarSample.collected_at_utc == collected_at,
                    ForecastSolarSample.forecast_time == forecast_time,
                ))
                if row is None:
                    raise ValueError("cannot save adjusted forecast for a missing raw forecast row")
                for name, value in values.items():
                    setattr(row, name, value)

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
        row.update(economic_period_values(
            row.get("plant_grid_import_total_kwh_period"),
            row.get("plant_grid_export_total_kwh_period"),
            row.get("plant_load_total_kwh_period"),
            tariffs,
            datetime.fromisoformat(collected[1]),
        ))
        self._add_pv_array_measurements(row, previous)
        with self.sessions.begin() as session:
            session.add(SigenStorModbusSample(**row))
            self._add_hourly_sample(session, row)
        previous.update({column_name(metric, unit): value for metric, (value, unit, _) in values.items()})
        previous["_collected_at_utc"] = collected[0]
        previous.update({column: row.get(column) for column in PV_ARRAY_POWER_COLUMNS})

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

        statement = sqlite_insert(hourly_table).values(hourly_values)
        update_values: dict[str, Any] = {
            "sample_count": hourly_table.c.sample_count + statement.excluded.sample_count,
        }
        for name in HOURLY_MEASUREMENT_COLUMNS:
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

    @heavy_work("hourly rollup range rebuild")
    def rebuild_hourly_rollup(self, start: date, end: date, timezone: ZoneInfo) -> None:
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
        target = SigenStorHourlySample.__table__
        with self.engine.begin() as connection:
            connection.execute(target.delete().where(
                target.c.hour_start_utc >= start_epoch,
                target.c.hour_start_utc < end_epoch,
            ))
            result = self._insert_hourly_rollup(connection, start_epoch, end_epoch)
        LOGGER.info("Hourly rollup rebuild inserted %s rows from %s through %s", result.rowcount, start, end)

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
            *aggregate_values,
        )
        if start_epoch is not None:
            source_query = source_query.where(source.c.collected_at_utc >= start_epoch)
        if end_epoch is not None:
            source_query = source_query.where(source.c.collected_at_utc < end_epoch)
        source_query = source_query.group_by(hour_start, tariff_band)
        statement = sqlite_insert(target).from_select(
            ("hour_start_utc", "import_tariff_band", "sample_count", *HOURLY_MEASUREMENT_COLUMNS),
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

@cache
def create_engine_for_database(path: str | Path) -> Engine:
    engine = create_engine(f"sqlite:///{Path(path)}", future=True)
    def is_read_query(statement: str) -> bool:
        return statement.lstrip().upper().startswith(("SELECT", "WITH", "EXPLAIN"))

    @event.listens_for(engine, "before_cursor_execute")
    def log_sql_start(connection, cursor, statement, parameters, context, executemany) -> None:
        if LOGGER.isEnabledFor(logging.DEBUG) and is_read_query(statement):
            context._solar_sql_started_at = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def log_sql_end(connection, cursor, statement, parameters, context, executemany) -> None:
        if hasattr(context, "_solar_sql_started_at"):
            elapsed = perf_counter() - context._solar_sql_started_at
            LOGGER.debug("SQL query (%.3fs): %s", elapsed, statement)
    return engine

def open_database(path: str | Path) -> SolarDatabase:
    engine = create_engine_for_database(path)
    _rename_forecast_solar_raw_columns(engine)
    _rename_sigenstor_battery_available_discharge_column(engine)
    inspector = inspect(engine)
    hourly_table_missing = not inspector.has_table(SigenStorHourlySample.__table__.name)
    # Create/migrate the raw source first. The hourly table mirrors its
    # measurement columns, so it must only be created after this step.
    Base.metadata.create_all(
        engine,
        tables=(
            SigenStorModbusSample.__table__,
            SigenStorDevice.__table__,
            DatabaseMetadata.__table__,
            ForecastSolarSample.__table__,
        ),
    )
    _add_missing_columns(engine, SigenStorModbusSample)
    _add_missing_columns(engine, SigenStorDevice)
    _add_missing_columns(engine, ForecastSolarSample)
    _drop_columns(engine, SigenStorModbusSample, ("raw_registers_json",))
    _drop_columns(engine, ForecastSolarSample, ("raw_json",))
    database = SolarDatabase(engine)
    if hourly_table_missing:
        SigenStorHourlySample.__table__.create(engine)
        database.backfill_hourly_rollup()
    else:
        _add_missing_columns(engine, SigenStorHourlySample)
    return database


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


def _add_missing_columns(engine: Engine, model: type[Base]) -> None:
    table = cast(Table, model.__table__)
    existing_columns = {column["name"] for column in inspect(engine).get_columns(table.name)}
    missing_columns = [column for column in table.columns if column.name not in existing_columns]
    if not missing_columns:
        return

    preparer = engine.dialect.identifier_preparer
    table_name = preparer.quote(table.name)
    with engine.begin() as connection:
        for column in missing_columns:
            column_name = preparer.quote(column.name)
            column_type = column.type.compile(dialect=engine.dialect)
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _rename_sigenstor_battery_available_discharge_column(engine: Engine) -> None:
    """Preserve inverter availability history under the plant-level name."""
    old_name = "inverter_battery_available_discharge_kwh"
    new_name = "plant_battery_available_discharge_kwh"
    preparer = engine.dialect.identifier_preparer
    for table_name in (SigenStorModbusSample.__table__.name, SigenStorHourlySample.__table__.name):
        if not inspect(engine).has_table(table_name):
            continue
        existing_columns = {
            column["name"] for column in inspect(engine).get_columns(table_name)
        }
        if old_name not in existing_columns or new_name in existing_columns:
            continue
        quoted_table = preparer.quote(table_name)
        quoted_old = preparer.quote(old_name)
        quoted_new = preparer.quote(new_name)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_old} TO {quoted_new}"
            )


def _rename_forecast_solar_raw_columns(engine: Engine) -> None:
    """Migrate the pre-adjustment forecast names without losing API values.

    SQLite's native rename retains both the values and the scaled-integer
    storage type.  This must run before ``create_all``/``_add_missing_columns``
    add the new adjusted columns.  A partially completed old deployment that
    already has both names is repaired by filling only absent raw values.
    """
    table = ForecastSolarSample.__table__
    if not inspect(engine).has_table(table.name):
        return
    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table.name)
    legacy_to_raw = {
        f"panel_{panel_id}_{measure}": f"panel_{panel_id}_raw_{measure}"
        for panel_id in PV_ARRAY_IDS
        for measure in ("watts", "watt_hours", "watt_hours_day")
    }
    with engine.begin() as connection:
        existing = {
            column["name"] for column in inspect(connection).get_columns(table.name)
        }
        for legacy, raw in legacy_to_raw.items():
            if legacy not in existing:
                continue
            quoted_legacy = preparer.quote(legacy)
            quoted_raw = preparer.quote(raw)
            if raw not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_legacy} TO {quoted_raw}"
                )
                existing.remove(legacy)
                existing.add(raw)
                continue

            # This state can only result from a previous non-atomic/manual
            # migration.  Preserve the raw column's explicit values and fill
            # only gaps from the legacy column before removing the duplicate.
            connection.exec_driver_sql(
                f"UPDATE {quoted_table} SET {quoted_raw} = {quoted_legacy} "
                f"WHERE {quoted_raw} IS NULL"
            )
            conflict = connection.exec_driver_sql(
                f"SELECT 1 FROM {quoted_table} WHERE {quoted_raw} IS NOT NULL "
                f"AND {quoted_legacy} IS NOT NULL AND {quoted_raw} != {quoted_legacy} LIMIT 1"
            ).first()
            if conflict is not None:
                raise RuntimeError(
                    f"refusing to drop conflicting forecast columns {legacy!r} and {raw!r}"
                )
            connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_legacy}")
            existing.remove(legacy)


def _drop_columns(engine: Engine, model: type[Base], column_names: tuple[str, ...]) -> None:
    table = cast(Table, model.__table__)
    table_name = table.name
    existing_columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
    columns_to_drop = [name for name in column_names if name in existing_columns]
    if not columns_to_drop:
        return

    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    with engine.begin() as connection:
        for name in columns_to_drop:
            connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {preparer.quote(name)}")
