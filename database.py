from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Float, Index, Integer, String, Table, create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from config import SolarArrayConfig
from datasheets import SIGENSTOR_MODEL_SPECIFICATIONS


def column_name(metric: str, unit: str) -> str:
    suffix = {"%": "percent", "kWh": "kwh", "kW": "kw", "V": "volts", "A": "amps", "Hz": "hz"}.get(unit, unit)
    metric = re.sub(r"[^a-zA-Z0-9_]", "_", metric).lower()
    return metric if metric.endswith(f"_{suffix}") else f"{metric}_{suffix}"


class Base(DeclarativeBase):
    pass


class SigenStorModbusSample(Base):
    __tablename__ = "sigenstor_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collected_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    collected_at_local: Mapped[str] = mapped_column(String, nullable=False)

    plant_grid_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_pv_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_battery_power_kw: Mapped[float | None] = mapped_column(Float)
    plant_battery_soc_percent: Mapped[float | None] = mapped_column(Float)
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
    plant_battery_charge_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_battery_charge_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    plant_battery_discharge_total_kwh: Mapped[float | None] = mapped_column(Float)
    plant_battery_discharge_total_kwh_period: Mapped[float | None] = mapped_column(Float)

    inverter_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_battery_power_kw: Mapped[float | None] = mapped_column(Float)
    inverter_battery_soc_percent: Mapped[float | None] = mapped_column(Float)
    inverter_battery_rated_capacity_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_available_discharge_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_avg_cell_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_battery_charge_daily_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_charge_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_battery_discharge_daily_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_battery_discharge_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv_daily_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_pv_daily_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv_total_kwh: Mapped[float | None] = mapped_column(Float)
    inverter_pv_total_kwh_period: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv1_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv2_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv3_current_amps: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_voltage_volts: Mapped[float | None] = mapped_column(Float)
    inverter_pv4_current_amps: Mapped[float | None] = mapped_column(Float)
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
    variable: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)


class ForecastSolarSample(Base):
    __tablename__ = "forecast_solar_samples"
    __table_args__ = (Index("idx_forecast_collection_time", "collection_guid", "forecast_time", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_guid: Mapped[str | None] = mapped_column(String(36))
    collected_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    collected_at_local: Mapped[str] = mapped_column(String, nullable=False)
    forecast_time: Mapped[str] = mapped_column(String, nullable=False)

    panel_1_watts: Mapped[float | None] = mapped_column(Float)
    panel_1_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_1_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_2_watts: Mapped[float | None] = mapped_column(Float)
    panel_2_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_2_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_3_watts: Mapped[float | None] = mapped_column(Float)
    panel_3_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_3_watt_hours_day: Mapped[float | None] = mapped_column(Float)
    panel_4_watts: Mapped[float | None] = mapped_column(Float)
    panel_4_watt_hours: Mapped[float | None] = mapped_column(Float)
    panel_4_watt_hours_day: Mapped[float | None] = mapped_column(Float)


CUMULATIVE_COLUMNS = (
    "plant_pv_daily_kwh", "plant_pv_total_kwh", "plant_load_daily_kwh", "plant_load_total_kwh",
    "plant_grid_import_total_kwh", "plant_grid_export_total_kwh", "plant_battery_charge_total_kwh",
    "plant_battery_discharge_total_kwh", "inverter_battery_charge_daily_kwh",
    "inverter_battery_discharge_daily_kwh", "inverter_pv_daily_kwh", "inverter_pv_total_kwh",
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
        return {column: getattr(row, column) for column in CUMULATIVE_COLUMNS if row is not None and getattr(row, column) is not None}

    def save_forecast(
        self,
        array: SolarArrayConfig,
        payload: dict,
        collected: tuple[str, str],
        collection_guid: str,
    ) -> int:
        result = payload.get("result", {})
        watts = result.get("watts", {})
        watt_hours = result.get("watt_hours", {})
        daily = result.get("watt_hours_day", {})
        prefix = array.panel
        fields = (f"{prefix}_watts", f"{prefix}_watt_hours", f"{prefix}_watt_hours_day")
        if any(field not in ForecastSolarSample.__table__.c for field in fields):
            raise ValueError(f"forecast panel {array.panel!r} is not declared in ForecastSolarSample")

        rows = 0
        with self.sessions.begin() as session:
            for forecast_time in sorted(set(watts) | set(watt_hours)):
                values: dict[str, Any] = {
                    "collection_guid": collection_guid,
                    "collected_at_utc": collected[0],
                    "collected_at_local": collected[1],
                    "forecast_time": forecast_time,
                    fields[0]: watts.get(forecast_time),
                    fields[1]: watt_hours.get(forecast_time),
                    fields[2]: daily.get(forecast_time[:10]),
                }
                row = session.scalar(select(ForecastSolarSample).where(
                    ForecastSolarSample.collection_guid == collection_guid,
                    ForecastSolarSample.forecast_time == forecast_time,
                ))
                if row is None:
                    session.add(ForecastSolarSample(**values))
                else:
                    for name, value in values.items():
                        setattr(row, name, value)
                rows += 1
        return rows

    def save_modbus_sample(self, values: dict[str, tuple[float, str, bool]], previous: dict[str, float], collected: tuple[str, str]) -> None:
        row: dict[str, Any] = {
            "collected_at_utc": collected[0],
            "collected_at_local": collected[1],
        }
        for metric, (value, unit, cumulative) in values.items():
            name = column_name(metric, unit)
            if name not in SigenStorModbusSample.__table__.c:
                raise ValueError(f"Modbus metric {metric!r} is not declared in ModbusSampleWide")
            row[name] = value
            if cumulative:
                row[f"{name}_period"] = value - previous[name] if name in previous and value >= previous[name] else None
        with self.sessions.begin() as session:
            session.add(SigenStorModbusSample(**row))
        previous.update({column_name(metric, unit): value for metric, (value, unit, _) in values.items()})

    def save_device_info(
        self,
        values: dict[str, tuple[float | str, str, bool]],
        collected: tuple[str, str],
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
                    variable=variable,
                    value=value,
                    unit=unit,
                ))
                changed += 1
        return changed


def open_database(path: str | Path) -> SolarDatabase:
    engine = create_engine(f"sqlite:///{Path(path)}", future=True)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine, SigenStorModbusSample)
    _add_missing_columns(engine, SigenStorDevice)
    _add_missing_columns(engine, ForecastSolarSample)
    _drop_columns(engine, SigenStorModbusSample, ("raw_registers_json",))
    _drop_columns(engine, ForecastSolarSample, ("raw_json",))
    return SolarDatabase(engine)


def device_value_text(value: float | str) -> str | None:
    if isinstance(value, str):
        cleaned = value.replace("\x00", "").strip()
        return cleaned if cleaned and cleaned != "0" else None
    if not math.isfinite(value) or value == 0:
        return None
    return format(value, ".15g")


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
