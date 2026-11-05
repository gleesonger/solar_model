from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import cast
from itertools import groupby
from time import perf_counter
import logging
import math
import cvxpy as cp
import pandas as pd
from solar_model.config import Config
from solar_model.time_utils import utc_instant
from solar_model.tariffs import BATTERY_EXPORT_LIMIT_KW_DEFAULT, MAXIMUM_BATTERY_KWH_DEFAULT, MINIMUM_BATTERY_KWH_DEFAULT, tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp
from solar_model.planning.projection_grid import projection_intervals as build_projection_intervals

from solar_model.planning.types import DeviceInfo, DeviceState, ScenarioData, NextAction, OperatingMode, SimulationResult
from solar_model.planning.system import allocate_energy_routes, system_limits

PROTECTION_APPROXIMATION_ERROR = 0.05  # Protection score points.
LOGGER = logging.getLogger(__name__)


def simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, scenarios: list[ScenarioData], config: Config, projection_starts_at: datetime) -> SimulationResult:
    """Run the model with performance and failure logging around the calculation."""
    begin = perf_counter()
    LOGGER.info("plan_model start: starts_at=%s scenarios=%d", projection_starts_at, len(scenarios))

    try:
        result = _simulate_energy_plan(device_info, device_state, scenarios, config, projection_starts_at)
        print(f"plan_model end: status=success elapsed_seconds={perf_counter() - begin:.3f}", flush=True)
        return result
    except Exception:
        LOGGER.exception("plan_model end: status=failed elapsed_seconds=%.3f", perf_counter() - begin)
        raise


def _simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, scenarios: list[ScenarioData], config: Config, projection_starts_at: datetime) -> SimulationResult:
    """Choose one shared first mode and optimise all scenarios in one solve."""
    if not scenarios:
        raise ValueError("at least one scenario is required")

    scenario_names = [scenario.name for scenario in scenarios]

    if any(not name.strip() for name in scenario_names) or len(scenario_names) != len(set(scenario_names)):
        raise ValueError("scenario names must be non-empty and unique")
    if any(not math.isfinite(s.probability) or s.probability < 0 for s in scenarios) or sum(s.probability for s in scenarios) <= 0:
        raise ValueError("scenario probabilities must be finite, non-negative and have a positive total")

    projection_starts_at = utc_instant(projection_starts_at)
    periods = _projection_intervals(config, projection_starts_at)
    visible_end = projection_starts_at + timedelta(hours=config.energy_plan.num_visible_projection_hours)
    visible_periods = sum(end <= visible_end for _, end in periods)
    durations = tuple((end - start).total_seconds() / 3600 for start, end in periods)
    # #### Build Model
    num_time_periods, scenario_count = len(durations), len(scenarios)
    theta_hrs = durations[0]
    plan_columns = ["starts_at", "ends_at", "minimum_battery_kwh", "maximum_battery_kwh", "tariff_name", "import_rate", "export_rate", "limit_battery_export_rate_to_grid_kw", "load_kwh", "solar_kwh"]
    initial_battery = device_state.battery_kwh
    if not math.isfinite(initial_battery) or initial_battery < 0:
        raise ValueError("initial battery energy must be finite and non-negative")
    limits = system_limits(device_info, theta_hrs)

    shape = (num_time_periods, scenario_count)
    solar_used = cp.Variable(shape, nonneg=True, name="solar_used")
    battery_charge_input = cp.Variable(shape, nonneg=True, name="battery_charge_input")
    grid_charge_ac = cp.Variable(shape, nonneg=True, name="grid_charge_ac")
    battery_discharge_output = cp.Variable(shape, nonneg=True, name="battery_discharge_output")
    grid_import = cp.Variable(shape, nonneg=True, name="grid_import")
    grid_export = cp.Variable(shape, nonneg=True, name="grid_export")
    battery_charging = cp.Variable(shape, boolean=True, name="battery_charging")
    grid_importing = cp.Variable(shape, boolean=True, name="grid_importing")

    # Three policies; charging and discharging each have a zero-rate variation.
    mode = cp.Variable(5, boolean=True, name="next_mode")
    self_consumption, charge_active, charge_hold, discharge_active, discharge_hold = [mode[i] for i in range(5)]
    charge_mode = charge_active + charge_hold

    next_action_force_battery_charge_rate = cp.Variable(nonneg=True, name="next_force_battery_charge_rate")
    next_action_force_battery_discharge_rate = cp.Variable(nonneg=True, name="next_force_battery_discharge_rate")
    constraints = [
        cp.sum(mode) == 1,
        next_action_force_battery_charge_rate >= config.energy_plan.min_forced_charge_power_kw * charge_active,
        next_action_force_battery_discharge_rate >= config.energy_plan.min_forced_discharge_power_kw * discharge_active,
        next_action_force_battery_charge_rate <= min(config.energy_plan.max_forced_charge_power_kw, limits.max_charge_kwh / theta_hrs) * charge_active,
        next_action_force_battery_discharge_rate <= min(config.energy_plan.max_forced_discharge_power_kw, limits.max_discharge_kwh / theta_hrs) * discharge_active,
    ]
    battery_level_eop = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="battery_level_eop")

    period_limits = [system_limits(device_info, duration) for duration in durations]

    # Assemble forecasts, tariffs and scheduled limits for each scenario.
    tariff_zone = ZoneInfo(config.timezone)
    dict_df_results = {scenario.name: pd.DataFrame(index=range(num_time_periods), columns=plan_columns) for scenario in scenarios}

    for t, (starts_at, ends_at) in enumerate(periods):
        # Tariffs and battery bounds use the interval's local start time.
        local = starts_at.astimezone(tariff_zone)
        import_rules, export_rules = tariff_rules_for_timestamp(config.tariffs, local)
        import_rule = tariff_rule(import_rules, local)

        limit_battery_export_rate_to_grid_kw = tariff_rule_rate(config.energy_plan.limit_battery_export_rate_to_grid_kw, local, BATTERY_EXPORT_LIMIT_KW_DEFAULT)
        minimum_battery_kwh = tariff_rule_rate(config.energy_plan.minimum_battery_kwh, local, MINIMUM_BATTERY_KWH_DEFAULT)
        maximum_battery_kwh = min(limits.capacity_kwh, tariff_rule_rate(config.energy_plan.maximum_battery_kwh, local, MAXIMUM_BATTERY_KWH_DEFAULT))
        export_rate = tariff_rule(export_rules, local).rate

        for scenario in scenarios:
            forecast_point = scenario.forecast_data.sum(starts_at, ends_at)
            dict_df_results[scenario.name].loc[t] = {
                "starts_at": starts_at,
                "ends_at": ends_at,

                "tariff_name": cast(str, import_rule.name),
                "import_rate": import_rule.rate,
                "export_rate": export_rate,

                "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw,
                "minimum_battery_kwh": minimum_battery_kwh,
                "maximum_battery_kwh": maximum_battery_kwh,

                "load_kwh": forecast_point.load_kwh,
                "solar_kwh": forecast_point.solar_kwh,
            }

    # Collect numeric inputs in period/scenario order.
    inputs = {
        column: [[float(df[column].iloc[t]) for df in dict_df_results.values()] for t in range(num_time_periods)]
        for column in plan_columns if column not in ("starts_at", "ends_at", "tariff_name")
    }

    # Reject invalid inputs before adding the physical constraints.
    if any(not math.isfinite(value) for rows in inputs.values() for row in rows for value in row):
        raise ValueError("planning inputs must be finite")

    if any(value < 0 for column in ("solar_kwh", "load_kwh", "limit_battery_export_rate_to_grid_kw") for row in inputs[column] for value in row):
        raise ValueError("solar, load and battery export limits must be non-negative")

    solar = inputs["solar_kwh"]
    demand = inputs["load_kwh"]
    eta_ac_dc = device_info.inverter_ac_to_dc_efficiency
    eta_dc_ac = device_info.inverter_dc_to_ac_efficiency
    eta_charge = device_info.battery_charge_efficiency
    eta_discharge = device_info.battery_discharge_efficiency

    # Work one period at a time: CVXPY accepts ordinary one-dimensional lists.
    for t, (duration, limit) in enumerate(zip(durations, period_limits)):
        output = device_info.inverter_max_active_power_kw * duration
        absorption = min(output, device_info.inverter_max_absorption_power_kw * duration)
        site_import_limit = device_info.grid_max_import_power_kw * duration
        import_limit = [min(load + absorption, site_import_limit) for load in demand[t]]

        solar_charge_dc = battery_charge_input[t, :] - eta_ac_dc * grid_charge_ac[t, :]
        gross_output_ac = (solar_used[t, :] - solar_charge_dc + battery_discharge_output[t, :]) * eta_dc_ac
        inverter_output = gross_output_ac - grid_charge_ac[t, :]
        battery_start = initial_battery if t == 0 else battery_level_eop[t - 1, :]

        constraints += [
            # PV availability and battery/inverter power limits.
            solar_used[t, :] <= solar[t],
            solar_charge_dc >= 0,
            solar_charge_dc <= solar_used[t, :],
            grid_charge_ac[t, :] <= absorption,
            grid_charge_ac[t, :] <= absorption * grid_importing[t, :],
            (solar_used[t, :] - solar_charge_dc) * eta_dc_ac <= demand[t] + output * (1 - grid_importing[t, :]),
            battery_charge_input[t, :] <= limit.max_charge_kwh * battery_charging[t, :],
            battery_discharge_output[t, :] <= limit.max_discharge_kwh * (1 - battery_charging[t, :]),
            gross_output_ac <= output,
            inverter_output >= -absorption,

            # Grid balance and mutually exclusive import/export.
            grid_export[t, :] - grid_import[t, :] == inverter_output - demand[t],
            grid_import[t, :] <= cp.multiply(import_limit, grid_importing[t, :]),
            grid_export[t, :] <= output * (1 - grid_importing[t, :]),

            # Stored-energy bounds and charging/discharging losses.
            battery_level_eop[t, :] >= inputs["minimum_battery_kwh"][t],
            battery_level_eop[t, :] <= inputs["maximum_battery_kwh"][t],
            battery_level_eop[t, :] == battery_start + eta_charge * battery_charge_input[t, :] - battery_discharge_output[t, :] / eta_discharge,
        ]

        # Battery export is min(discharge, grid export). Cap either quantity;
        # the other branch must still allow PV exports above the battery cap.
        for s, rate in enumerate(inputs["limit_battery_export_rate_to_grid_kw"][t]):
            cap = rate * duration
            if cap < eta_dc_ac * limit.max_discharge_kwh:
                discharge_limited = cp.Variable(boolean=True)
                constraints += [
                    eta_dc_ac * battery_discharge_output[t, s] <= cap + (eta_dc_ac * limit.max_discharge_kwh - cap) * (1 - discharge_limited),
                    grid_export[t, s] <= cap + (output - cap) * discharge_limited,
                ]

    # First action is an executable mode and power profile. SOC targets are
    # controller triggers derived after solving, not inverter cut-off settings.
    requested_charge = next_action_force_battery_charge_rate * theta_hrs
    requested_discharge = next_action_force_battery_discharge_rate * theta_hrs
    constraints += [battery_charging[0, :] >= charge_mode + discharge_hold, battery_charging[0, :] <= 1 - discharge_active]

    output_ac = device_info.inverter_max_active_power_kw * theta_hrs
    output_dc = output_ac / eta_dc_ac
    absorption = min(output_ac, device_info.inverter_max_absorption_power_kw * theta_hrs)
    charge_limit, discharge_limit = limits.max_charge_kwh, limits.max_discharge_kwh
    charge_room = max(0., limits.capacity_kwh - initial_battery) / eta_charge
    discharge_room = initial_battery * eta_discharge
    for s in range(scenario_count):
        pv, load = solar[0][s], demand[0][s]
        charge, discharge, used, grid_charge = battery_charge_input[0, s], battery_discharge_output[0, s], solar_used[0, s], grid_charge_ac[0, s]

        # Self-consumption: PV serves load, surplus charges, battery covers demand.
        pv_load = min(pv, load / eta_dc_ac, output_dc)
        passive_charge = min(pv - pv_load, charge_limit, charge_room)
        passive_discharge = min(load / eta_dc_ac - pv_load, discharge_limit, output_dc - pv_load, discharge_room)
        passive_solar = min(pv, passive_charge + output_dc - passive_discharge)
        _when(self_consumption, charge, passive_charge, charge_limit, constraints)
        _when(self_consumption, discharge, passive_discharge, discharge_limit, constraints)
        _when(self_consumption, used, passive_solar, pv, constraints)
        _when(self_consumption, grid_charge, 0., absorption, constraints)

        # Charge: PV serves the household first; remaining PV and grid can charge.
        pv_surplus = pv - pv_load
        grid_headroom = max(0., device_info.grid_max_import_power_kw * theta_hrs - max(0., load - pv_load * eta_dc_ac))
        charge_ceiling = min(charge_limit, charge_room, pv_surplus + absorption * eta_ac_dc, pv_surplus + grid_headroom * eta_ac_dc)
        forced_charge = _minimum(requested_charge, charge_ceiling, charge_limit, constraints)
        solar_charge = _minimum(forced_charge, pv_surplus, charge_limit, constraints)
        forced_grid_charge = (forced_charge - solar_charge) / eta_ac_dc
        charge_solar = _minimum(solar_charge + output_dc, pv, charge_limit + output_dc, constraints)
        _when(charge_active, charge, forced_charge, charge_limit, constraints)
        _when(charge_active, discharge, 0., discharge_limit, constraints)
        _when(charge_active, used, charge_solar, pv, constraints)
        _when(charge_active, grid_charge, forced_grid_charge, absorption, constraints)

        # Zero charging rate holds both battery directions while PV serves load.
        _when(charge_hold, charge, 0., charge_limit, constraints)
        _when(charge_hold, discharge, 0., discharge_limit, constraints)
        _when(charge_hold, used, min(pv, output_dc), pv, constraints)
        _when(charge_hold, grid_charge, 0., absorption, constraints)

        # Discharge, PV first: PV gets AC capacity before battery output.
        discharge_solar = min(pv, output_dc)
        discharge_ceiling = min(discharge_limit, output_dc - discharge_solar, discharge_room)
        forced_discharge = _minimum(requested_discharge, discharge_ceiling, discharge_limit, constraints)
        _when(discharge_active, charge, 0., charge_limit, constraints)
        _when(discharge_active, discharge, forced_discharge, discharge_limit, constraints)
        _when(discharge_active, used, discharge_solar, pv, constraints)
        _when(discharge_active, grid_charge, 0., absorption, constraints)

        # At zero discharge, PV serves load, charges battery, then exports.
        _when(discharge_hold, charge, passive_charge, charge_limit, constraints)
        _when(discharge_hold, discharge, 0., discharge_limit, constraints)
        _when(discharge_hold, used, min(pv, passive_charge + output_dc), pv, constraints)
        _when(discharge_hold, grid_charge, 0., absorption, constraints)

        # The shared discharge ceiling must respect the export allowance.
        export_allowance = max(0., load - discharge_solar * eta_dc_ac) + inputs["limit_battery_export_rate_to_grid_kw"][0][s] * theta_hrs
        if discharge_ceiling * eta_dc_ac > export_allowance:
            constraints.append(next_action_force_battery_discharge_rate * theta_hrs * eta_dc_ac <= export_allowance)

    net_cost_flows = [
        cp.multiply([row[s] for row in inputs["import_rate"]], grid_import[:, s]) - cp.multiply([row[s] for row in inputs["export_rate"]], grid_export[:, s])
        for s in range(scenario_count)
    ]
    battery_levels = [battery_level_eop[:, s] for s in range(scenario_count)]

    # #### Optimise Model

    score = score_simulation(net_cost_flows, battery_levels, scenarios, device_info, config, constraints, projection_starts_at, durations)
    problem = cp.Problem(cp.Maximize(score), constraints)

    steps = [(end - start).total_seconds() / 60 for start, end in periods]
    summary = ", ".join(f"{minutes:g}*{sum(1 for _ in group)}" for minutes, group in groupby(steps))
    LOGGER.info(
        "plan_model projection: periods=%d visible_periods=%d lookahead_periods=%d scenarios=%d time_steps=[%s]",
        len(periods),
        visible_periods,
        len(periods) - visible_periods,
        len(scenarios),
        summary,
    )
    metrics = problem.size_metrics
    booleans = sum(variable.size for variable in problem.variables() if variable.attributes["boolean"])
    LOGGER.info(
        "plan_model complexity: variables=%d booleans=%d constraints=%d parameters=%d",
        metrics.num_scalar_variables,
        booleans,
        metrics.num_scalar_eq_constr + metrics.num_scalar_leq_constr,
        sum(parameter.size for parameter in problem.parameters()),
    )
    problem.solve(solver=cp.HIGHS, warm_start=False)
    print(
        f"plan_model solve: status={problem.status} compile_seconds={problem.compilation_time:.3f} solver_seconds={problem.solver_stats.solve_time or 0.:.3f} objective={problem.value}",
        flush=True,
    )
    if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
        raise ValueError(f"unable to find a feasible energy plan: {problem.status}")

    # #### Model to Results

    selected = max(range(5), key=lambda i: mode.value[i])
    selected_mode = (OperatingMode.SELF_CONSUMPTION, OperatingMode.CHARGE_PV_FIRST, OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST)[selected]
    charge_rate = max(0., float(next_action_force_battery_charge_rate.value)) if selected == 1 else 0.
    discharge_rate = max(0., float(next_action_force_battery_discharge_rate.value)) if selected == 3 else 0.
    target = None
    if selected == 1:
        target = min(100., max(0., max(float(battery_level_eop.value[0, s]) for s in range(scenario_count)) / limits.capacity_kwh * 100))
    elif selected == 3:
        target = min(100., max(0., min(float(battery_level_eop.value[0, s]) for s in range(scenario_count)) / limits.capacity_kwh * 100))
    next_action = NextAction(
        mode=selected_mode,
        charge_power_limit_kw=charge_rate if selected_mode == OperatingMode.CHARGE_PV_FIRST else device_info.inverter_battery_charge_power_kw,
        discharge_power_limit_kw=discharge_rate if selected_mode == OperatingMode.DISCHARGE_PV_FIRST else (0. if selected_mode == OperatingMode.CHARGE_PV_FIRST else device_info.inverter_battery_discharge_power_kw),
        target_soc_percent=target,
    )

    protection_value = _protection_value(config, projection_starts_at, sum(durations))
    terminal_rate = _terminal_rate(config, periods[-1][1]) * eta_discharge * eta_dc_ac
    score_segments = _protection_segments(limits.capacity_kwh, config.energy_plan.preferred_reserve_kwh)
    for s, (scenario_name, df) in enumerate(dict_df_results.items()):

        battery_end_values = battery_levels[s].value.tolist()
        df["battery_start_kwh"] = [device_state.battery_kwh, *battery_end_values[:-1]]

        solar_values, load_values = [row[s] for row in solar], [row[s] for row in demand]
        used = [min(pv, max(0., value)) for pv, value in zip(solar_values, solar_used.value[:, s].tolist())]
        charge = [max(0., value) for value in battery_charge_input.value[:, s].tolist()]
        discharge = [max(0., value) for value in battery_discharge_output.value[:, s].tolist()]
        grid_charging = [max(0., value) for value in grid_charge_ac.value[:, s].tolist()]
        flows = allocate_energy_routes(solar_values, load_values, used, charge, grid_charging, discharge, device_info, durations)
        for name, value in flows.items():
            df[f"{name}_kwh"] = value

        df["solar_to_inverter_kwh"] = used
        df["battery_to_inverter_kwh"] = discharge
        df["inverter_dc_to_ac_kwh"] = df["solar_to_load_kwh"] + df["solar_to_grid_kwh"] + df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]
        df["inverter_ac_to_dc_kwh"] = [value * eta_ac_dc for value in grid_charging]
        df["grid_import_kwh"] = df["grid_to_load_kwh"] + grid_charging
        df["grid_export_kwh"] = df["solar_to_grid_kwh"] + df["battery_to_grid_kwh"]
        df["battery_end_kwh"] = battery_end_values
        df["battery_charging"] = battery_charging.value[:, s]
        df["import_cost"] = df["grid_import_kwh"] * df["import_rate"]
        df["export_revenue"] = df["grid_export_kwh"] * df["export_rate"]
        df["net_cost"] = df["import_cost"] - df["export_revenue"]
        protection = [min(slope * energy + intercept for slope, intercept in score_segments) for energy in battery_end_values]
        df["cost_score"] = -df["net_cost"]
        df["protection_score"] = [protection_value * min(100., max(0., value)) / 100 * duration / sum(durations) for value, duration in zip(protection, durations)]
        df["terminal_score"] = 0.
        df.loc[df.index[-1], "terminal_score"] = terminal_rate * battery_end_values[-1]
        df["total_score"] = df["cost_score"] + df["protection_score"] + df["terminal_score"]

        # Independently check reconstructed routes against every solved boundary.
        errors = {
            "solar": df["solar_kwh"] - (df["solar_to_load_kwh"] + df["solar_to_grid_kwh"]) / eta_dc_ac - df["solar_to_battery_kwh"] / eta_charge - df["solar_clipped_kwh"],
            "load": df["load_kwh"] - df["solar_to_load_kwh"] - df["battery_to_load_kwh"] - df["grid_to_load_kwh"],
            "battery": df["battery_end_kwh"] - df["battery_start_kwh"] - df["solar_to_battery_kwh"] - df["grid_to_battery_kwh"] + (df["battery_to_load_kwh"] + df["battery_to_grid_kwh"]) / (eta_dc_ac * eta_discharge),
            "grid import": df["grid_import_kwh"] - grid_import.value[:, s],
            "grid export": df["grid_export_kwh"] - grid_export.value[:, s],
        }
        invalid = {name: error.index[error.abs() > .001].tolist() for name, error in errors.items() if (error.abs() > .001).any()}
        if invalid:
            raise ValueError(f"energy balance check failed for {scenario_name}: {invalid}")

    return SimulationResult(
        next_action=next_action,
        results={name: frame.iloc[:visible_periods].copy() for name, frame in dict_df_results.items()},
        lookahead_results={name: frame.iloc[visible_periods:].reset_index(drop=True) for name, frame in dict_df_results.items()},
    )



def _when(mask: cp.Expression, value: cp.Expression, target: cp.Expression | float, upper: float, constraints: list[cp.Constraint]) -> None:
    """mask=1 means value=target; mask=0 permits their full physical range."""
    slack = upper * (1 - mask)
    constraints += [value - target <= slack, target - value <= slack]


def _minimum(requested: cp.Expression, ceiling: float, upper: float, constraints: list[cp.Constraint], *, lower: float = 0.) -> cp.Expression | float:
    """Exact min(requested, ceiling) for lower <= requested <= upper."""
    if ceiling <= lower:
        return ceiling
    if ceiling >= upper:
        return requested
    result = cp.Variable(nonneg=True)
    at_ceiling = cp.Variable(boolean=True)
    constraints += [
        result <= requested,
        result <= ceiling,
        result >= requested - upper * at_ceiling,
        result >= ceiling * at_ceiling,
    ]
    return result


def _projection_intervals(config: Config, starts_at: datetime) -> list[tuple[datetime, datetime]]:
    """Keep the first action at 15 minutes, then follow UTC-aligned boundaries."""
    s = config.energy_plan
    timesteps = s.optimiser.mix_integer_linear_prog.projection_resolution_mins
    if s.num_projection_hours <= 0 or not timesteps or any(type(minutes) is not int or minutes <= 0 for minutes in timesteps):
        raise ValueError("projection duration and resolution must be greater than zero")
    if sum(timesteps) < s.num_projection_hours * 60:
        raise ValueError("projection_resolution_mins must cover num_projection_hours")
    return build_projection_intervals(starts_at, s.num_projection_hours, s.num_visible_projection_hours, timesteps)



def score_simulation(
    net_cost_flows: list[list[cp.Expression] | cp.Expression],  # Scenario x Time
    battery_levels: list[list[cp.Expression] | cp.Expression],
    scenarios: list[ScenarioData],
    device: DeviceInfo,
    config: Config,
    constraints: list[cp.Constraint],
    projection_starts_at: datetime,
    durations: list[float] | tuple[float, ...] | None = None,
) -> cp.Expression:
    """Maximise protection reward minus electricity cost across the full horizon.

    R is stored kWh measured from zero; X is non-negative protection_preference.
    The maximum reward is X/100 * R * average import rate * horizon hours/24.
    Price is time-weighted over the next 24 elapsed hours, independent of load.
    Each scenario earns that reward times its average diminishing-return score
    divided by 100. Supplied durations include the hidden lookahead periods, so
    the protection reward per elapsed hour is unchanged. Terminal energy earns
    the following 12 hours' import/export midpoint, after discharge losses.
    """
    projection_starts_at = utc_instant(projection_starts_at)
    settings = config.energy_plan
    projection_hours = sum(durations) if durations is not None else settings.num_projection_hours
    protection_value = _protection_value(config, projection_starts_at, projection_hours)
    terminal_rate = _terminal_rate(config, projection_starts_at + timedelta(hours=projection_hours)) * device.battery_discharge_efficiency * device.inverter_dc_to_ac_efficiency
    scores = []
    for scenario, costs, levels in zip(scenarios, net_cost_flows, battery_levels):
        cost, protection = score_scenario(costs, levels, device, config, constraints, durations)
        scores.append(scenario.probability * (-cost + protection_value * protection / 100 + terminal_rate * levels[-1]))

    return cp.sum(scores) / sum(s.probability for s in scenarios)


def _terminal_rate(config: Config, ends_at: datetime) -> float:
    """Mean hourly import/export midpoint over the 12 hours after the model end."""
    end = utc_instant(ends_at)
    zone = ZoneInfo(config.timezone)
    rates = []
    for hour in range(12):
        local = (end + timedelta(hours=hour)).astimezone(zone)
        imports, exports = tariff_rules_for_timestamp(config.tariffs, local)
        rates.append((tariff_rule(imports, local).rate + tariff_rule(exports, local).rate) / 2)
    return sum(rates) / len(rates)


def _protection_value(config: Config, starts_at: datetime, projection_hours: float) -> float:
    settings = config.energy_plan
    return settings.protection_preference / 100 * settings.preferred_reserve_kwh * _reference_import_rate(config, starts_at, starts_at + timedelta(hours=24)) * projection_hours / 24


def _reference_import_rate(config: Config, starts_at: datetime, ends_at: datetime) -> float:
    """Approximate the reference price using one tariff sample per elapsed hour."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (starts_at, ends_at)):
        raise ValueError("reference tariff range must be timezone-aware")
    cursor, end = starts_at.astimezone(timezone.utc), ends_at.astimezone(timezone.utc)
    duration = (end - cursor).total_seconds()
    if duration <= 0:
        raise ValueError("reference tariff end must be after start")
    zone = ZoneInfo(config.timezone)
    weighted_rate = 0.
    while cursor < end:
        local = cursor.astimezone(zone)
        import_rules, _ = tariff_rules_for_timestamp(config.tariffs, local)
        rule = tariff_rule(import_rules, local)
        range_end = min(end, cursor + timedelta(hours=1))
        weighted_rate += rule.rate * (range_end - cursor).total_seconds()
        cursor = range_end
    return max(0., weighted_rate / duration)


def score_scenario(
    costs: list[cp.Expression] | cp.Expression,
    levels: list[cp.Expression] | cp.Expression,
    device: DeviceInfo,
    config: Config,
    constraints: list[cp.Constraint],
    durations: list[float] | None = None,
) -> tuple[cp.Expression, cp.Expression]:
    """Return total cost and average diminishing-return protection from zero kWh."""
    capacity = device.system_battery_capacity_kwh * device.battery_state_of_health_percent / 100
    reserve = config.energy_plan.preferred_reserve_kwh
    if not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("preferred_reserve_kwh must be finite and greater than zero")
    segments = _protection_segments(capacity, reserve)
    cost = cp.sum(costs)
    stored_energy = levels if isinstance(levels, cp.Expression) else cp.hstack(levels)
    protection = cp.Variable(stored_energy.shape, nonneg=True)
    # Linear bounds approximate diminishing returns: 0 empty, 70 at reserve,
    # approaching 100 as energy increases. Maximisation lifts scores onto the curve.
    constraints.append(protection <= 100)
    constraints.extend(protection <= slope * stored_energy + intercept for slope, intercept in segments)
    durations = durations if durations is not None else [1.] * stored_energy.size
    return cost, cp.sum(cp.multiply(protection, durations)) / sum(durations)


def battery_protection_score(energy: float, reserve: float) -> float:
    """Agreed reserve utility: 0 at empty, 70 at reserve, then diminishing returns."""
    if not math.isfinite(energy) or energy < 0 or not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("stored energy must be finite and non-negative; reserve must be finite and positive")
    if energy <= reserve:
        return 100 * (1 - 0.3 ** (energy / reserve))
    return 70 + 30 * (energy - reserve) / (energy + reserve)


def _protection_segments(capacity: float, reserve: float) -> list[tuple[float, float]]:
    """Concave chord approximation with a bounded maximum score shortfall."""
    if capacity == 0:
        return [(0., 0.)]
    knots = sorted({0., min(reserve, capacity), capacity})
    pending = list(zip(knots[:-1], knots[1:]))
    segments = []
    while pending:
        left, right = pending.pop()
        yl, yr = battery_protection_score(left, reserve), battery_protection_score(right, reserve)
        slope = (yr - yl) / (right - left)
        intercept = yl - slope * left
        if slope == 0:
            segments.append((slope, intercept))
            continue
        if right <= reserve:
            point = reserve * math.log(slope * reserve / (-100 * math.log(0.3))) / math.log(0.3)
        else:
            point = math.sqrt(60 * reserve / slope) - reserve
        point = min(right, max(left, point))
        gap = battery_protection_score(point, reserve) - (slope * point + intercept)
        if gap > PROTECTION_APPROXIMATION_ERROR and left < point < right:
            pending.extend([(left, point), (point, right)])
        else:
            segments.append((slope, intercept))
    return segments
