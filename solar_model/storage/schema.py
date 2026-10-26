from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import Column, Index, Integer, String, Table, Text, func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

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
    house_load_kwh_period: Mapped[float | None] = mapped_column(Float)
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

class ForecastHouseLoadSample(Base):
    """One immutable hourly house-demand forecast per configured local day."""

    __tablename__ = "forecast_house_load"
    __table_args__ = (Index("idx_house_load_forecast_time", "forecast_time"),)
    target_day: Mapped[str] = mapped_column(String, primary_key=True)
    forecast_time: Mapped[str] = mapped_column(EpochSeconds, primary_key=True)
    collected_at_utc: Mapped[str] = mapped_column(EpochSeconds, nullable=False)
    load_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)


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

HOURLY_MEASUREMENT_COLUMNS = tuple(
    column.name
    for column in SigenStorModbusSample.__table__.columns
    if column.name not in {"collected_at_utc", "import_tariff_band"}
)

HOURLY_GRID_POWER_COLUMNS = ("plant_grid_import_power_kw", "plant_grid_export_power_kw")

HOURLY_VALID_COUNT_COLUMNS = {
    name: f"{name}_sample_count" for name in HOURLY_MEASUREMENT_COLUMNS
    if name not in CUMULATIVE_COLUMNS and not name.endswith("_period") and name != "plant_grid_power_kw"
}

HOURLY_ROLLUP_MARKER = "hourly_rollup_initialized_v3"

class SigenStorHourlySample(Base):
    __table__ = Table(
        "sigenstor_hourly",
        Base.metadata,
        Column("hour_start_utc", EpochSeconds, primary_key=True),
        # A non-null sentinel permits an hourly row even if a source sample is
        # missing its tariff band, and keeps the composite key unambiguous.
        Column("import_tariff_band", Text, primary_key=True, nullable=False),
        Column("sample_count", Integer, nullable=False),
        Column("grid_power_sample_count", Integer, nullable=True),
        *(Column(name, Float, nullable=True) for name in HOURLY_GRID_POWER_COLUMNS),
        *(Column(name, Integer, nullable=True) for name in HOURLY_VALID_COUNT_COLUMNS.values()),
        *(
            Column(column.name, column.type, nullable=True)
            for column in SigenStorModbusSample.__table__.columns
            if column.name not in {"collected_at_utc", "import_tariff_band"}
        ),
    )
