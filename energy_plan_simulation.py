"""Read-only self-consumption simulation for SigEnergy planning inputs."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from collections.abc import Iterable
from typing import cast

import cvxpy as cp
import pandas as pd
from config import Config
from economics import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp

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
class ForecastPoint:
    solar_kwh: float
    load_kwh: float


@dataclass(frozen=True)
class ForecastInterval:
    starts_at: datetime
    ends_at: datetime
    kwh: float


class ForecastData:
    def __init__(self, solar: Iterable[ForecastInterval], load: Iterable[ForecastInterval]) -> None:
        self.solar = self._validate_intervals(solar, "solar")
        self.load = self._validate_intervals(load, "load")

    def sum(self, start_time: datetime, end_time: datetime) -> ForecastPoint:
        start = start_time
        end = end_time
        if end <= start:
            raise ValueError("forecast end_time must be after start_time")

        return ForecastPoint(solar_kwh=self._sum_intervals(self.solar, start, end), load_kwh=self._sum_intervals(self.load, start, end))

    @staticmethod
    def _validate_intervals(intervals: Iterable[ForecastInterval], name: str) -> tuple[ForecastInterval, ...]:
        ordered = tuple(sorted(intervals, key=lambda interval: interval.starts_at))
        if not ordered:
            raise ValueError(f"{name} forecast must contain at least one interval")
        for previous, current in zip(ordered, ordered[1:]):
            if current.starts_at < previous.ends_at:
                raise ValueError(f"{name} forecast intervals must not overlap")
            if current.starts_at > previous.ends_at:
                raise ValueError(f"{name} forecast intervals contain a gap")
        if any(interval.ends_at <= interval.starts_at for interval in ordered):
            raise ValueError(f"{name} forecast intervals must end after they start")
        return ordered

    @staticmethod
    def _sum_intervals(intervals: tuple[ForecastInterval, ...], start: datetime, end: datetime) -> float:
        cursor = start
        total_kwh = 0.0
        for interval in intervals:
            if interval.ends_at <= cursor:
                continue
            if interval.starts_at > cursor:
                break
            overlap_start = max(cursor, interval.starts_at)
            overlap_end = min(end, interval.ends_at)
            if overlap_end > overlap_start:
                interval_seconds = (interval.ends_at - interval.starts_at).total_seconds()
                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                total_kwh += interval.kwh * overlap_seconds / interval_seconds
                cursor = overlap_end
            if cursor >= end:
                return total_kwh
        raise ValueError("forecast range is outside the known data or contains a gap; extrapolation is not supported")


def simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, forecast: ForecastData, config: Config, projection_starts_at: datetime) -> pd.DataFrame:

    num_time_periods: int = int(config.energy_plan.num_projection_hours * 60) // config.energy_plan.projection_resolution_mins
    theta_hrs = config.energy_plan.projection_resolution_mins / 60

    if num_time_periods <= 0:
        raise ValueError("num_projection_hours must be greater than zero")
    df = pd.DataFrame(index=range(num_time_periods), columns=["starts_at", "ends_at", "minimum_battery_kwh", "maximum_battery_kwh", "tariff_name", "import_rate", "export_rate", "limit_battery_export_rate_to_grid_kw", "load_kwh", "solar_kwh"])

    if not (0 < device_info.charge_efficiency <= 1) or not (0 < device_info.discharge_efficiency <= 1):
        raise ValueError("battery efficiencies must be between 0 and 1 (inclusive)")

    capacity_kwh = device_info.system_battery_capacity_kwh * device_info.battery_state_of_health_percent / 100
    max_charge_kwh = min(device_info.inverter_battery_charge_power_kw, device_info.inverter_max_absorption_power_kw) * theta_hrs
    max_discharge_kwh = min(device_info.inverter_battery_discharge_power_kw, device_info.inverter_max_active_power_kw) * theta_hrs

    if capacity_kwh <= 0 or max_charge_kwh < 0 or max_discharge_kwh < 0:
        raise ValueError("battery capacity and charge/discharge limits must be non-negative")

    solar_to_load = cp.Variable(num_time_periods, nonneg=True, name="solar_to_load")
    solar_to_battery = cp.Variable(num_time_periods, nonneg=True, name="solar_to_battery")
    solar_to_grid = cp.Variable(num_time_periods, nonneg=True, name="solar_to_grid")
    solar_clipped = cp.Variable(num_time_periods, nonneg=True, name="solar_clipped")
    battery_to_load = cp.Variable(num_time_periods, nonneg=True, name="battery_to_load")
    battery_to_grid = cp.Variable(num_time_periods, nonneg=True, name="battery_to_grid")
    grid_to_load = cp.Variable(num_time_periods, nonneg=True, name="grid_to_load")
    grid_to_battery = cp.Variable(num_time_periods, nonneg=True, name="grid_to_battery")
    battery_charging = cp.Variable(num_time_periods, boolean=True, name="battery_charging")

    charge_loss_factor = 1 / device_info.charge_efficiency - 1
    discharge_loss_factor = 1 / device_info.discharge_efficiency - 1

    solar_to_battery_loss = charge_loss_factor * solar_to_battery
    grid_to_battery_loss = charge_loss_factor * grid_to_battery

    battery_to_ac_loss = discharge_loss_factor * (battery_to_load + battery_to_grid)

    constraints = []
    net_cost_flows = []
    battery_levels = []
    battery_level_worth = []
    battery_level_eop = device_state.battery_kwh

    # Set up the LP problem for each time period
    for t in range(num_time_periods):
        starts_at = projection_starts_at + timedelta(minutes=t * config.energy_plan.projection_resolution_mins)
        ends_at = starts_at + timedelta(minutes=config.energy_plan.projection_resolution_mins)
        forecast_point = forecast.sum(starts_at, ends_at)

        import_rules, export_rules = tariff_rules_for_timestamp(config.tariffs, starts_at)
        import_rule = tariff_rule(import_rules, starts_at)
        limit_battery_export_rate_to_grid_kw = tariff_rule_rate(config.energy_plan.limit_battery_export_rate_to_grid_kw, starts_at,9999)

        solar_kwh = forecast_point.solar_kwh
        load_kwh = forecast_point.load_kwh

        minimum_battery_kwh = tariff_rule(config.energy_plan.minimum_battery_kwh, starts_at).rate
        maximum_battery_kwh = min(capacity_kwh, tariff_rule(config.energy_plan.maximum_battery_kwh, starts_at).rate)

        import_rate = import_rule.rate
        export_rate = tariff_rule(export_rules, starts_at).rate

        battery_level_bop = battery_level_eop

        inverter_flows_dc_to_ac = solar_to_load[t] + solar_to_grid[t] + battery_to_load[t] + battery_to_grid[t]
        inverter_flows_ac_to_dc = grid_to_battery[t] + grid_to_battery_loss[t]

        constraints += [
            solar_to_load[t] + solar_to_battery[t] + solar_to_battery_loss[t] + solar_to_grid[t] + solar_clipped[t] == solar_kwh,

            solar_to_load[t] + battery_to_load[t] + grid_to_load[t] == load_kwh,

            solar_to_battery[t] + solar_to_battery_loss[t] + grid_to_battery[t] + grid_to_battery_loss[t] <= max_charge_kwh * battery_charging[t],

            battery_to_load[t] + battery_to_grid[t] <= max_discharge_kwh * (1 - battery_charging[t]),

            # inverter clipping
            (inverter_flows_dc_to_ac+inverter_flows_ac_to_dc) <= device_info.inverter_max_active_power_kw * theta_hrs,

            grid_to_battery[t] + grid_to_battery_loss[t] <= device_info.inverter_max_absorption_power_kw * theta_hrs,
            battery_to_grid[t] <= limit_battery_export_rate_to_grid_kw * theta_hrs,
        ]

        battery_level_eop = battery_level_bop + (
            (solar_to_battery[t] + grid_to_battery[t])
            - (battery_to_load[t] + battery_to_grid[t] + battery_to_ac_loss[t])
        )

        battery_levels.append(battery_level_eop)
        battery_level_worth.append(battery_level_eop*import_rate*theta_hrs) # We need to scale battery to a euro figure for the optimiser to work, import_rate isnt perfect, as our goal is backup duration rather than profit, but it is likely a decent simplified approach for most tariffs/load patterns

        constraints += [minimum_battery_kwh <= battery_level_eop, battery_level_eop <= maximum_battery_kwh]

        net_cost_flows.append(
            import_rate * (grid_to_load[t] + grid_to_battery[t] + grid_to_battery_loss[t])
            - export_rate * (solar_to_grid[t] + battery_to_grid[t])
        )

        df.loc[t] = {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "tariff_name": cast(str, import_rule.name),
            "import_rate": import_rate,
            "export_rate": export_rate,
            "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw,
            "minimum_battery_kwh": minimum_battery_kwh,
            "maximum_battery_kwh": maximum_battery_kwh,
            "load_kwh": load_kwh,
            "solar_kwh": solar_kwh,
        }

    weights = config.energy_plan.optimisation_weights

    problem = cp.Problem(cp.Maximize(
        - cp.sum(net_cost_flows) * weights.maximise_profit
        + cp.sum(battery_level_worth) * weights.maximise_battery)
    , constraints)

    problem.solve(solver=cp.HIGHS)

    if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
        raise ValueError(f"unable to find a feasible energy plan: {problem.status}")

    battery_end_values = [level.value for level in battery_levels]
    df["battery_start_kwh"] = [device_state.battery_kwh,*battery_end_values[:-1]]
    df["solar_to_load_kwh"] = solar_to_load.value
    df["solar_to_battery_kwh"] = solar_to_battery.value
    df["solar_to_battery_loss_kwh"] = solar_to_battery_loss.value
    df["grid_to_battery_loss_kwh"] = grid_to_battery_loss.value
    df["solar_to_grid_kwh"] = solar_to_grid.value
    df["solar_to_inverter_kwh"] = df["solar_to_load_kwh"] + df["solar_to_battery_kwh"] + df["solar_to_battery_loss_kwh"] + df["solar_to_grid_kwh"]
    df["solar_clipped_kwh"] = solar_clipped.value
    df["battery_to_load_kwh"] = battery_to_load.value
    df["battery_to_grid_kwh"] = battery_to_grid.value
    df["battery_to_inverter_kwh"] = df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]
    df["battery_to_ac_loss_kwh"] = battery_to_ac_loss.value
    df["grid_to_load_kwh"] = grid_to_load.value
    df["grid_to_battery_kwh"] = grid_to_battery.value
    df["inverter_dc_to_ac_kwh"] = df["solar_to_load_kwh"] + df["solar_to_grid_kwh"] + df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]
    df["inverter_ac_to_dc_kwh"] = grid_to_battery.value + grid_to_battery_loss.value # type: ignore
    df["grid_import_kwh"] = grid_to_load.value + grid_to_battery.value + grid_to_battery_loss.value # type: ignore
    df["grid_export_kwh"] = df["solar_to_grid_kwh"] + df["battery_to_grid_kwh"]
    df["battery_end_kwh"] = battery_end_values
    df["battery_charging"] = battery_charging.value

    df["import_cost"] = df["grid_import_kwh"] * df["import_rate"]
    df["export_revenue"] = df["grid_export_kwh"] * df["export_rate"]
    df["net_cost"] = df["import_cost"] - df["export_revenue"]

    solar_accounted_kwh = df["solar_to_load_kwh"] + df["solar_to_battery_kwh"] + df["solar_to_battery_loss_kwh"] + df["solar_to_grid_kwh"] + df["solar_clipped_kwh"]
    load_accounted_kwh = df["solar_to_load_kwh"] + df["battery_to_load_kwh"] + df["grid_to_load_kwh"]

    solar_balance_error = (df["solar_kwh"] - solar_accounted_kwh).abs()
    load_balance_error = (df["load_kwh"] - load_accounted_kwh).abs()
    tolerance_kwh = 0.001
    if (solar_balance_error > tolerance_kwh).any() or (load_balance_error > tolerance_kwh).any():
        invalid_periods = df.index[(solar_balance_error > tolerance_kwh) | (load_balance_error > tolerance_kwh)].tolist()
        raise ValueError(f"energy balance check failed for periods: {invalid_periods}")

    return df
