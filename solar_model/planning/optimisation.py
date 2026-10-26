from __future__ import annotations
from datetime import datetime, timedelta
from typing import cast
import math
import cvxpy as cp
import pandas as pd
from solar_model.config import Config
from solar_model.time_utils import add_elapsed
from solar_model.tariffs import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp

from solar_model.planning.types import DeviceInfo, DeviceState, ScenarioData, NextAction, OperatingMode, SimulationResult
from solar_model.planning.system import system_limits

MIN_FORCE_RATE_KW = 0.1  # Minimum positive command: 100 watts.
PROTECTION_APPROXIMATION_ERROR = 0.05  # Protection score points.
REFERENCE_TOLERANCE = 1e-6  # Cost/score units for reference tie-breaks and zero ranges.


def simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, scenarios: list[ScenarioData], config: Config, projection_starts_at: datetime) -> SimulationResult:
    """Build, optimise and report scenarios sharing the next action."""

    # #### Build Model

    if not scenarios:
        raise ValueError("at least one scenario is required")

    scenario_names = [scenario.name for scenario in scenarios]

    if any(not name.strip() for name in scenario_names) or len(scenario_names) != len(set(scenario_names)):
        raise ValueError("scenario names must be non-empty and unique")
    if any(not math.isfinite(s.probability) or s.probability < 0 for s in scenarios) or sum(s.probability for s in scenarios) <= 0:
        raise ValueError("scenario probabilities must be finite, non-negative and have a positive total")

    num_time_periods: int = int(config.energy_plan.num_projection_hours * 60) // config.energy_plan.projection_resolution_mins
    scenario_count = len(scenarios)
    theta_hrs = config.energy_plan.projection_resolution_mins / 60

    if num_time_periods <= 0:
        raise ValueError("num_projection_hours must be greater than zero")

    plan_columns = ["starts_at", "ends_at", "minimum_battery_kwh", "maximum_battery_kwh", "tariff_name", "import_rate", "export_rate", "limit_battery_export_rate_to_grid_kw", "load_kwh", "solar_kwh"]
    dict_df_results = {scenario.name: pd.DataFrame(index=range(num_time_periods), columns=plan_columns) for scenario in scenarios}

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

    forcing = cp.Variable(2, boolean=True, name="next_forcing_active")

    next_action_force_battery_charge_rate = cp.Variable(nonneg=True, name="next_force_battery_charge_rate")
    next_action_force_battery_discharge_rate = cp.Variable(nonneg=True, name="next_force_battery_discharge_rate")
    next_action_charge_cut_off_soc_percent = cp.Variable(nonneg=True, name="next_charge_cut_off_soc_percent")
    next_action_discharge_cut_off_soc_percent = cp.Variable(nonneg=True, name="next_discharge_cut_off_soc_percent")

    constraints = [
        cp.sum(forcing) <= 1,
        next_action_force_battery_charge_rate >= MIN_FORCE_RATE_KW * forcing[0],
        next_action_force_battery_discharge_rate >= MIN_FORCE_RATE_KW * forcing[1],
        next_action_force_battery_charge_rate <= max_charge_kwh / theta_hrs * forcing[0],
        next_action_force_battery_discharge_rate <= max_discharge_kwh / theta_hrs * forcing[1],
        next_action_discharge_cut_off_soc_percent <= 100,
        next_action_charge_cut_off_soc_percent <= 100,
    ]
    # Shared first-step energy, clipped at the relevant SOC stopping point.
    big_m = (capacity_kwh + abs(device_state.battery_kwh)) / min(device_info.charge_efficiency, device_info.discharge_efficiency) + max(max_charge_kwh, max_discharge_kwh)
    forced_charge_energy = _energy_until_soc(
        next_action_force_battery_charge_rate * theta_hrs,
        (capacity_kwh * next_action_charge_cut_off_soc_percent / 100 - device_state.battery_kwh) / device_info.charge_efficiency,
        big_m, constraints,
    )
    forced_discharge_energy = _energy_until_soc(
        next_action_force_battery_discharge_rate * theta_hrs,
        (device_state.battery_kwh - capacity_kwh * next_action_discharge_cut_off_soc_percent / 100) * device_info.discharge_efficiency,
        big_m, constraints,
    )
    net_cost_flows: list[list[cp.Expression]] = [[] for _ in scenarios]
    battery_levels: list[list[cp.Expression]] = [[] for _ in scenarios]
    battery_level_eop: list[cp.Expression | float] = [device_state.battery_kwh for _ in scenarios]

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

        for s, scenario in enumerate(scenarios):
            forecast_point = scenario.forecast_data.sum(starts_at, ends_at)
            solar_kwh = forecast_point.solar_kwh
            load_kwh = forecast_point.load_kwh
            battery_level_bop = battery_level_eop[s]
            inverter_flows_dc_to_ac = solar_to_load[t, s] + solar_to_grid[t, s] + battery_to_load[t, s] + battery_to_grid[t, s]
            inverter_flows_ac_to_dc = grid_to_battery[t, s] + grid_to_battery_loss[t, s]
            charging = battery_charging[t, s]
            battery_charge_input = solar_to_battery[t, s] + solar_to_battery_loss[t, s] + inverter_flows_ac_to_dc
            battery_discharge_output = battery_to_load[t, s] + battery_to_grid[t, s]
            if t == 0:
                constraints += [
                    charging >= forcing[0], charging <= 1 - forcing[1],
                    grid_to_battery[t, s] <= max_charge_kwh * forcing[0],
                    battery_to_grid[t, s] <= max_discharge_kwh * forcing[1],
                    battery_charge_input >= forced_charge_energy,
                    battery_charge_input <= forced_charge_energy + max_charge_kwh * (1 - forcing[0]),
                    battery_discharge_output >= forced_discharge_energy,
                    battery_discharge_output <= forced_discharge_energy + max_discharge_kwh * (1 - forcing[1]),
                ]
            constraints += [
                solar_to_load[t, s] + solar_to_battery[t, s] + solar_to_battery_loss[t, s] + solar_to_grid[t, s] + solar_clipped[t, s] == solar_kwh,
                solar_to_load[t, s] + battery_to_load[t, s] + grid_to_load[t, s] == load_kwh,
                battery_charge_input <= max_charge_kwh * charging,
                battery_discharge_output <= max_discharge_kwh * (1 - charging),
                inverter_flows_dc_to_ac + inverter_flows_ac_to_dc <= device_info.inverter_max_active_power_kw * theta_hrs,
                grid_to_battery[t, s] + grid_to_battery_loss[t, s] <= device_info.inverter_max_absorption_power_kw * theta_hrs,
                battery_to_grid[t, s] <= limit_battery_export_rate_to_grid_kw * theta_hrs,
            ]
            battery_level_eop[s] = battery_level_bop + solar_to_battery[t, s] + grid_to_battery[t, s] - battery_to_load[t, s] - battery_to_grid[t, s] - battery_to_ac_loss[t, s]
            battery_levels[s].append(battery_level_eop[s])
            constraints += [minimum_battery_kwh <= battery_level_eop[s], battery_level_eop[s] <= maximum_battery_kwh]
            net_cost_flows[s].append(import_rate * (grid_to_load[t, s] + grid_to_battery[t, s] + grid_to_battery_loss[t, s]) - export_rate * (solar_to_grid[t, s] + battery_to_grid[t, s]))
            dict_df_results[scenario.name].loc[t] = {"starts_at": starts_at, "ends_at": ends_at, "tariff_name": cast(str, import_rule.name), "import_rate": import_rate, "export_rate": export_rate, "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw, "minimum_battery_kwh": minimum_battery_kwh, "maximum_battery_kwh": maximum_battery_kwh, "load_kwh": load_kwh, "solar_kwh": solar_kwh}

    # #### Optimise Model

    score = score_simulation(net_cost_flows, battery_levels, scenarios, device_info, config, constraints)
    problem = cp.Problem(cp.Maximize(score), constraints)

    problem.solve(solver=cp.HIGHS)

    if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
        raise ValueError(f"unable to find a feasible energy plan: {problem.status}")

    # #### Model to Results

    mode = (OperatingMode.CHARGE_PV_FIRST if next_action_force_battery_charge_rate.value >= MIN_FORCE_RATE_KW / 2
            else OperatingMode.DISCHARGE_PV_FIRST if next_action_force_battery_discharge_rate.value >= MIN_FORCE_RATE_KW / 2
            else OperatingMode.SELF_CONSUMPTION)
    
    next_action = NextAction(
        mode = mode,
        force_battery_charge_rate=float(next_action_force_battery_charge_rate.value),
        force_battery_discharge_rate=float(next_action_force_battery_discharge_rate.value),
        charge_cut_off_soc_percent=float(next_action_charge_cut_off_soc_percent.value),
        discharge_cut_off_soc_percent=float(next_action_discharge_cut_off_soc_percent.value),
    
    )
    
    flows = {
        "solar_to_load": solar_to_load,
        "solar_to_battery": solar_to_battery,
        "solar_to_battery_loss": solar_to_battery_loss,
        "grid_to_battery_loss": grid_to_battery_loss,
        "solar_to_grid": solar_to_grid,
        "solar_clipped": solar_clipped,
        "battery_to_load": battery_to_load,
        "battery_to_grid": battery_to_grid,
        "battery_to_ac_loss": battery_to_ac_loss,
        "grid_to_load": grid_to_load,
        "grid_to_battery": grid_to_battery,
    }

    for s, (scenario_name, df) in enumerate(dict_df_results.items()):

        battery_end_values = [level.value for level in battery_levels[s]]
        df["battery_start_kwh"] = [device_state.battery_kwh, *battery_end_values[:-1]]

        for name, value in flows.items():
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

        # check
        solar_accounted_kwh = df["solar_to_load_kwh"] + df["solar_to_battery_kwh"] + df["solar_to_battery_loss_kwh"] + df["solar_to_grid_kwh"] + df["solar_clipped_kwh"]
        load_accounted_kwh = df["solar_to_load_kwh"] + df["battery_to_load_kwh"] + df["grid_to_load_kwh"]
        invalid_periods = df.index[((df["solar_kwh"] - solar_accounted_kwh).abs() > 0.001) | ((df["load_kwh"] - load_accounted_kwh).abs() > 0.001)].tolist()
        if invalid_periods:
            raise ValueError(f"energy balance check failed for {scenario_name} periods: {invalid_periods}")
        
    return SimulationResult(
        next_action=next_action,
        results=dict_df_results,
    )


def score_simulation(
        net_cost_flows: list[list[cp.Expression]], # Scenario x Time
        battery_levels: list[list[cp.Expression]],
        scenarios: list[ScenarioData],
        device: DeviceInfo,
        config: Config,
        constraints: list[cp.Constraint]
    ) -> cp.Expression:
    """Give higher scores to plans that cost less and keep more battery reserve.

    Compute a cost score and a battery score for each scenario, mix them using
    the configured weights, then sum the scenario scores times their probabilities.

    Cost is the sum of net costs across all time steps. Battery protection is
    scored at each step and averaged: usable energy is zero at the discharge
    cut-off, earns 70 points at the preferred reserve, and approaches 100 with
    diminishing returns above reserve. Linear segments approximate this curve.

    Money and battery points need a common scale. First solve for the cheapest
    plan and the plan with the highest average protection, using probability-
    weighted results across scenarios. These two plans set the scoring endpoints:
    cost scores 100 at the cheapest cost and 0 at the protection-first cost;
    battery scores 0 at the cheapest plan's protection and 100 at the best protection.
    Ties favour more protection for the cheapest plan, or less cost for the other.

    Every scenario uses these same endpoints, so an individual score can fall
    outside 0–100. This function adds scoring constraints and runs the reference
    solves; the caller then maximises the returned combined score.

    Issues with this approach:
     - It does not pick up on small differences, for example if the high and low
        battery/protection levels are almost the same then a person would treat them as the same
        but the model will give the range 100 of a score difference
     - We would need some real world way to link them:
        A) A person's utility function for cost and protection - Annoying to calibrate
        B) Convert protection into money terms, eg annual allowance for proection,
            but this seems very sensitive to the input and may not yield deseired outcome
        C) Maybe a joint utility function, user provides the joint measure of comparison, but this would need a UI interface
    """
    scenario_costs = {}
    scenario_protection = {}
    for scenario, costs, levels in zip(scenarios, net_cost_flows, battery_levels):
        scenario_costs[scenario.name], scenario_protection[scenario.name] = score_scenario(
            costs, levels, device, config, constraints,
        )

    expected_cost = cp.sum([s.probability * scenario_costs[s.name] for s in scenarios])
    expected_protection = cp.sum([s.probability * scenario_protection[s.name] for s in scenarios])

    # Find the cheapest plan; among equally cheap plans, prefer more battery reserve.
    _solve_reference(cp.Minimize(expected_cost), constraints)
    cheapest = float(expected_cost.value)
    _solve_reference(cp.Maximize(expected_protection), constraints + [expected_cost <= cheapest + REFERENCE_TOLERANCE])
    c0, h0 = float(expected_cost.value), float(expected_protection.value)

    # Find the best-protected plan; among equally protected plans, prefer lower cost.
    _solve_reference(cp.Maximize(expected_protection), constraints)
    safest = float(expected_protection.value)
    _solve_reference(cp.Minimize(expected_cost), constraints + [expected_protection >= safest - REFERENCE_TOLERANCE])
    c1, h1 = float(expected_cost.value), float(expected_protection.value)

    weights = config.energy_plan.optimisation_weights
    preference = weights.maximise_battery / (weights.maximise_profit + weights.maximise_battery)
    # If the goals tie or only one matters, keep that goal at its best reference value.
    if c1 - c0 <= REFERENCE_TOLERANCE:
        constraints += [expected_cost <= c0 + REFERENCE_TOLERANCE,
                        expected_protection >= h1 - REFERENCE_TOLERANCE]
    elif preference == 0 or h1 - h0 <= REFERENCE_TOLERANCE:
        constraints += [expected_cost <= c0 + REFERENCE_TOLERANCE,
                        expected_protection >= h0 - REFERENCE_TOLERANCE]
    elif preference == 1:
        constraints += [expected_protection >= h1 - REFERENCE_TOLERANCE,
                        expected_cost <= c1 + REFERENCE_TOLERANCE]
    # Convert each scenario to the same scales, mix cost and battery scores,
    # then combine the scenarios using their probabilities.
    scenario_scores = {}
    for scenario in scenarios:
        financial_score = (cp.Constant(100.) if c1 - c0 <= REFERENCE_TOLERANCE
                           else 100 * (c1 - scenario_costs[scenario.name]) / (c1 - c0))
        protection_score = (cp.Constant(100.) if h1 - h0 <= REFERENCE_TOLERANCE
                            else 100 * (scenario_protection[scenario.name] - h0) / (h1 - h0))
        scenario_scores[scenario.name] = (1 - preference) * financial_score + preference * protection_score
    return cp.sum([s.probability * scenario_scores[s.name] for s in scenarios])


def score_scenario(
        costs: list[cp.Expression],
        levels: list[cp.Expression],
        device: DeviceInfo,
        config: Config,
        constraints: list[cp.Constraint],
    ) -> tuple[cp.Expression, cp.Expression]:
    """Return total cost and average battery protection for one scenario.

    Add the battery-utility constraints here; score_simulation puts both values
    on common reference scales and combines them with the other scenarios.
    """
    # Only energy above the physical discharge cut-off counts towards the reserve.
    capacity = device.system_battery_capacity_kwh * device.battery_state_of_health_percent / 100
    floor = capacity * device.battery_discharge_cut_off_soc_percent / 100
    reserve = config.energy_plan.preferred_reserve_kwh
    if not math.isfinite(floor) or not 0 <= floor <= capacity:
        raise ValueError("physical discharge cut-off must be between 0 and 100 percent")
    if not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("preferred_reserve_kwh must be finite and greater than zero")
    segments = _protection_segments(capacity - floor, reserve)
    # Add up this scenario's cost, then score how well its battery stays filled.
    cost = cp.sum(costs)
    if capacity == floor:
        return cost, cp.Constant(0.)
    usable = cp.hstack(levels) - floor
    if floor > 0:
        # Preserve existing feasible trajectories below the physical cut-off:
        # they have zero usable energy, rather than a negative protection score.
        above_floor = cp.Variable(len(levels), boolean=True)
        available = cp.Variable(len(levels), nonneg=True)
        constraints += [available >= usable,
                        available <= usable + floor * (1 - above_floor),
                        available <= (capacity - floor) * above_floor]
        usable = available
    protection = cp.Variable(len(levels), nonneg=True)
    # Linear bounds approximate diminishing returns: 0 empty, 70 at reserve,
    # approaching 100 as energy increases. Maximisation lifts scores onto the curve.
    constraints.append(protection <= 100)
    constraints.extend(protection <= slope * usable + intercept for slope, intercept in segments)
    return cost, cp.sum(protection) / len(levels)


def battery_protection_score(energy: float, reserve: float) -> float:
    """Agreed reserve utility: 0 at empty, 70 at reserve, then diminishing returns."""
    if not math.isfinite(energy) or energy < 0 or not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("usable energy must be finite and non-negative; reserve must be finite and positive")
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


def _solve_reference(objective: cp.Minimize | cp.Maximize, constraints: list[cp.Constraint]) -> None:
    problem = cp.Problem(objective, constraints)
    problem.solve(solver=cp.HIGHS)
    if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
        raise ValueError(f"unable to find a feasible energy plan for scoring reference: {problem.status}")


def _energy_until_soc(requested: cp.Expression, headroom: cp.Expression, big_m: float,constraints: list[cp.Constraint]) -> cp.Variable:
    """Exact min(requested, max(0, headroom)): stop at or beyond the SOC cut-off."""
    
    energy = cp.Variable(nonneg=True)
    stopped, at_soc, full_rate = cp.Variable(3, boolean=True)
    constraints += [
        stopped + at_soc + full_rate == 1,
        energy <= requested,
        energy <= big_m * (1 - stopped), headroom <= big_m * (1 - stopped),
        energy >= headroom - big_m * (1 - at_soc),
        energy <= headroom + big_m * (1 - at_soc),
        energy >= requested - big_m * (1 - full_rate),
        headroom >= requested - big_m * (1 - full_rate),
    ]
    return energy
