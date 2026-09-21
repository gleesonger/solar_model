"""Read-only self-consumption simulation for SigEnergy planning inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import cast

import numpy as np
from scipy.optimize import linprog
from config import TariffsConfig
from economics import tariff_rule, tariff_rules_for_timestamp


@dataclass
class DeviceInfo:
    inverter_model: str
    inverter_serial_number: str
    inverter_firmware: str
    inverter_rated_power_kw: float
    inverter_max_apparent_power_kva: float
    inverter_max_active_power_kw: float
    inverter_max_absorption_power_kw: float
    inverter_battery_charge_power_kw: float
    inverter_battery_discharge_power_kw: float
    rated_grid_voltage_v: float
    rated_grid_frequency_hz: float
    battery_pack_count: float
    pv_string_count: float
    mppt_count: float
    system_battery_capacity_kwh: float
    battery_charge_cut_off_soc_percent: float
    battery_discharge_cut_off_soc_percent: float
    battery_state_of_health_percent: float
    maximum_pv_input_power_kw: float
    charge_efficiency: float
    discharge_efficiency: float

@dataclass
class DeviceState:
    battery_kwh: float


@dataclass(frozen=True)
class InputParameters:
    num_projection_hours: int
    projection_step_mins: int
    num_hours_to_backup: float


@dataclass(init=False)
class ForecastFigures:
    starts_at: datetime
    ends_at: datetime
    solar_kwh: float
    load_kwh: float

    @property
    def duration_hours(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 3600


@dataclass(init=False)
class TimePeriodState(ForecastFigures):
    battery_start_kwh: float

    solar_to_battery_kwh: float
    solar_to_grid_kwh: float
    solar_to_load_kwh: float
    solar_to_inverter_kwh: float

    battery_to_load_kwh: float
    battery_to_grid_kwh: float
    battery_to_inverter_kwh: float

    inverter_dc_to_ac_kwh: float
    inverter_ac_to_dc_kwh: float

    grid_import_kwh: float
    grid_export_kwh: float

    battery_end_kwh: float

    tariff_name: str
    import_rate: float
    export_rate: float
    import_cost: float
    export_revenue: float
    net_cost: float


def simulate_energy_plan(
    device_info: DeviceInfo,
    device_state: DeviceState,
    input_parameters: InputParameters,
    forecast_figures: ForecastFigures,
    tariffs: TariffsConfig,
) -> list[TimePeriodState]:

    num_time_periods: int = int(input_parameters.num_hours_to_backup * 60) // input_parameters.projection_step_mins

    time_periods = [ TimePeriodState() for t in range(num_time_periods) ]

    # Assign load & forecast figures to each time period
    for t in range(num_time_periods):
        tp = time_periods[t]
        tp.starts_at = forecast_figures.starts_at + timedelta(minutes=t * input_parameters.projection_step_mins)
        tp.ends_at = tp.starts_at + timedelta(minutes=input_parameters.projection_step_mins)
        tp.solar_kwh = forecast_figures.solar_kwh * (input_parameters.projection_step_mins / 60) / num_time_periods
        tp.load_kwh = forecast_figures.load_kwh * (input_parameters.projection_step_mins / 60) / num_time_periods

    # Assign tariff rates to each time period
    for t in range(num_time_periods):
        tp = time_periods[t]
        import_rules, export_rules = tariff_rules_for_timestamp(tariffs, tp.starts_at)
        import_rule = tariff_rule(import_rules, tp.starts_at)
        tp.import_rate = import_rule.rate
        tp.tariff_name = cast(str, import_rule.name)
        tp.export_rate = tariff_rule(export_rules, tp.starts_at).rate

    # We have the choice to meet load from solar, or use the grid and store the battery
    # the decision is mostly
