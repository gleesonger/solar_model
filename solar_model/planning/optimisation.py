from __future__ import annotations
from datetime import datetime, timedelta
from collections.abc import Sequence
from typing import cast
import cvxpy as cp
import pandas as pd
from solar_model.config import Config
from solar_model.time_utils import add_elapsed
from solar_model.tariffs import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp

from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData
from solar_model.planning.system import system_limits
from solar_model.planning.scoring import total_score


def simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, forecasts: Sequence[ForecastData], config: Config, projection_starts_at: datetime) -> dict[str, pd.DataFrame]:
    """Solve independent scenario trajectories in one problem and return numerical reports.

    First actions are not yet shared across scenarios; see the plan-model design.
    """

    if not forecasts:
        raise ValueError("at least one forecast scenario is required")

    scenario_names = [forecast.name for forecast in forecasts]

    if any(not name.strip() for name in scenario_names) or len(scenario_names) != len(set(scenario_names)):
        raise ValueError("forecast scenario names must be non-empty and unique")

    num_time_periods: int = int(config.energy_plan.num_projection_hours * 60) // config.energy_plan.projection_resolution_mins
    scenario_count = len(forecasts)
    theta_hrs = config.energy_plan.projection_resolution_mins / 60

    if num_time_periods <= 0:
        raise ValueError("num_projection_hours must be greater than zero")

    plan_columns = ["starts_at", "ends_at", "minimum_battery_kwh", "maximum_battery_kwh", "tariff_name", "import_rate", "export_rate", "limit_battery_export_rate_to_grid_kw", "load_kwh", "solar_kwh"]
    dict_df_results = {forecast.name: pd.DataFrame(index=range(num_time_periods), columns=plan_columns) for forecast in forecasts}

    limits = system_limits(device_info, theta_hrs)
    capacity_kwh = limits.capacity_kwh
    max_charge_kwh = limits.max_charge_kwh
    max_discharge_kwh = limits.max_discharge_kwh

    solar_to_load = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="solar_to_load")
    solar_to_battery = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="solar_to_battery")
    solar_to_grid = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="solar_to_grid")
    solar_clipped = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="solar_clipped")
    battery_to_load = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="battery_to_load")
    battery_to_grid = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="battery_to_grid")
    grid_to_load = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="grid_to_load")
    grid_to_battery = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="grid_to_battery")
    battery_charging = cp.Variable((num_time_periods, scenario_count), boolean=True, name="battery_charging")

    charge_loss_factor = limits.charge_loss_factor
    discharge_loss_factor = limits.discharge_loss_factor

    solar_to_battery_loss = charge_loss_factor * solar_to_battery
    grid_to_battery_loss = charge_loss_factor * grid_to_battery

    battery_to_ac_loss = discharge_loss_factor * (battery_to_load + battery_to_grid)

    constraints = []
    net_cost_flows = []
    battery_levels: list[list[cp.Expression]] = [[] for _ in forecasts]
    battery_level_worth = []
    battery_level_eop: list[cp.Expression | float] = [device_state.battery_kwh for _ in forecasts]

    # Set up the LP problem for each time period
    for t in range(num_time_periods):
        starts_at = add_elapsed(projection_starts_at, timedelta(minutes=t * config.energy_plan.projection_resolution_mins))
        ends_at = add_elapsed(starts_at, timedelta(minutes=config.energy_plan.projection_resolution_mins))
        import_rules, export_rules = tariff_rules_for_timestamp(config.tariffs, starts_at)
        import_rule = tariff_rule(import_rules, starts_at)
        limit_battery_export_rate_to_grid_kw = tariff_rule_rate(config.energy_plan.limit_battery_export_rate_to_grid_kw, starts_at,9999)

        minimum_battery_kwh = tariff_rule(config.energy_plan.minimum_battery_kwh, starts_at).rate
        maximum_battery_kwh = min(capacity_kwh, tariff_rule(config.energy_plan.maximum_battery_kwh, starts_at).rate)

        import_rate = import_rule.rate
        export_rate = tariff_rule(export_rules, starts_at).rate

        for s, forecast in enumerate(forecasts):
            forecast_point = forecast.sum(starts_at, ends_at)
            solar_kwh = forecast_point.solar_kwh
            load_kwh = forecast_point.load_kwh
            battery_level_bop = battery_level_eop[s]
            inverter_flows_dc_to_ac = solar_to_load[t, s] + solar_to_grid[t, s] + battery_to_load[t, s] + battery_to_grid[t, s]
            inverter_flows_ac_to_dc = grid_to_battery[t, s] + grid_to_battery_loss[t, s]
            constraints += [
                solar_to_load[t, s] + solar_to_battery[t, s] + solar_to_battery_loss[t, s] + solar_to_grid[t, s] + solar_clipped[t, s] == solar_kwh,
                solar_to_load[t, s] + battery_to_load[t, s] + grid_to_load[t, s] == load_kwh,
                solar_to_battery[t, s] + solar_to_battery_loss[t, s] + grid_to_battery[t, s] + grid_to_battery_loss[t, s] <= max_charge_kwh * battery_charging[t, s],
                battery_to_load[t, s] + battery_to_grid[t, s] <= max_discharge_kwh * (1 - battery_charging[t, s]),
                inverter_flows_dc_to_ac + inverter_flows_ac_to_dc <= device_info.inverter_max_active_power_kw * theta_hrs,
                grid_to_battery[t, s] + grid_to_battery_loss[t, s] <= device_info.inverter_max_absorption_power_kw * theta_hrs,
                battery_to_grid[t, s] <= limit_battery_export_rate_to_grid_kw * theta_hrs,
            ]
            battery_level_eop[s] = battery_level_bop + solar_to_battery[t, s] + grid_to_battery[t, s] - battery_to_load[t, s] - battery_to_grid[t, s] - battery_to_ac_loss[t, s]
            battery_levels[s].append(battery_level_eop[s])
            battery_level_worth.append(battery_level_eop[s] * import_rate * theta_hrs)
            constraints += [minimum_battery_kwh <= battery_level_eop[s], battery_level_eop[s] <= maximum_battery_kwh]
            net_cost_flows.append(import_rate * (grid_to_load[t, s] + grid_to_battery[t, s] + grid_to_battery_loss[t, s]) - export_rate * (solar_to_grid[t, s] + battery_to_grid[t, s]))
            dict_df_results[forecast.name].loc[t] = {"starts_at": starts_at, "ends_at": ends_at, "tariff_name": cast(str, import_rule.name), "import_rate": import_rate, "export_rate": export_rate, "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw, "minimum_battery_kwh": minimum_battery_kwh, "maximum_battery_kwh": maximum_battery_kwh, "load_kwh": load_kwh, "solar_kwh": solar_kwh}

    weights = config.energy_plan.optimisation_weights

    problem = cp.Problem(
        cp.Maximize(total_score(net_cost_flows, battery_level_worth, scenario_count, weights)),
        constraints,
    )

    problem.solve(solver=cp.HIGHS)

    if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
        raise ValueError(f"unable to find a feasible energy plan: {problem.status}")

    for s, forecast in enumerate(forecasts):
        df = dict_df_results[forecast.name]

        battery_end_values = [level.value for level in battery_levels[s]]
        df["battery_start_kwh"] = [device_state.battery_kwh, *battery_end_values[:-1]]

        for name, value in {"solar_to_load": solar_to_load, "solar_to_battery": solar_to_battery, "solar_to_battery_loss": solar_to_battery_loss, "grid_to_battery_loss": grid_to_battery_loss, "solar_to_grid": solar_to_grid, "solar_clipped": solar_clipped, "battery_to_load": battery_to_load, "battery_to_grid": battery_to_grid, "battery_to_ac_loss": battery_to_ac_loss, "grid_to_load": grid_to_load, "grid_to_battery": grid_to_battery}.items():
            df[f"{name}_kwh"] = value.value[:, s]
        df["solar_to_inverter_kwh"] = df["solar_to_load_kwh"] + df["solar_to_battery_kwh"] + df["solar_to_battery_loss_kwh"] + df["solar_to_grid_kwh"]
        df["battery_to_inverter_kwh"] = df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]
        df["inverter_dc_to_ac_kwh"] = df["solar_to_load_kwh"] + df["solar_to_grid_kwh"] + df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]
        df["inverter_ac_to_dc_kwh"] = df["grid_to_battery_kwh"] + df["grid_to_battery_loss_kwh"]
        df["grid_import_kwh"] = df["grid_to_load_kwh"] + df["grid_to_battery_kwh"] + df["grid_to_battery_loss_kwh"]
        df["grid_export_kwh"] = df["solar_to_grid_kwh"] + df["battery_to_grid_kwh"]
        df["battery_end_kwh"] = battery_end_values
        df["battery_charging"] = battery_charging.value[:, s]
        df["import_cost"] = df["grid_import_kwh"] * df["import_rate"]
        df["export_revenue"] = df["grid_export_kwh"] * df["export_rate"]
        df["net_cost"] = df["import_cost"] - df["export_revenue"]
        solar_accounted_kwh = df["solar_to_load_kwh"] + df["solar_to_battery_kwh"] + df["solar_to_battery_loss_kwh"] + df["solar_to_grid_kwh"] + df["solar_clipped_kwh"]
        load_accounted_kwh = df["solar_to_load_kwh"] + df["battery_to_load_kwh"] + df["grid_to_load_kwh"]
        invalid_periods = df.index[((df["solar_kwh"] - solar_accounted_kwh).abs() > 0.001) | ((df["load_kwh"] - load_accounted_kwh).abs() > 0.001)].tolist()
        if invalid_periods:
            raise ValueError(f"energy balance check failed for {forecast.name} periods: {invalid_periods}")
    return dict_df_results
