from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import cast
from itertools import groupby
from time import perf_counter
import logging
import math
import cvxpy as cp
import numpy as np
import pandas as pd
from solar_model.config import Config
from solar_model.time_utils import utc_instant
from solar_model.tariffs import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp

from solar_model.planning.types import DeviceInfo, DeviceState, ScenarioData, NextAction, OperatingMode, SimulationResult
from solar_model.planning.system import system_limits

MIN_FORCE_RATE_KW = 0.1  # Minimum positive command: 100 watts.
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
    """Build a fresh model, solve it and extract the scenario results."""
    if not scenarios:
        raise ValueError("at least one scenario is required")

    scenario_names = [scenario.name for scenario in scenarios]

    if any(not name.strip() for name in scenario_names) or len(scenario_names) != len(set(scenario_names)):
        raise ValueError("scenario names must be non-empty and unique")
    if any(not math.isfinite(s.probability) or s.probability < 0 for s in scenarios) or sum(s.probability for s in scenarios) <= 0:
        raise ValueError("scenario probabilities must be finite, non-negative and have a positive total")

    projection_starts_at = utc_instant(projection_starts_at)
    periods = _projection_intervals(config, projection_starts_at)
    durations = tuple((end - start).total_seconds() / 3600 for start, end in periods)
    # #### Build Model
    num_time_periods, scenario_count = len(durations), len(scenarios)
    theta_hrs = durations[0]
    plan_columns = ["starts_at", "ends_at", "minimum_battery_kwh", "maximum_battery_kwh", "tariff_name", "import_rate", "export_rate", "limit_battery_export_rate_to_grid_kw", "load_kwh", "solar_kwh"]
    initial_battery = device_state.battery_kwh
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
    big_m = (capacity_kwh + abs(initial_battery)) / min(device_info.charge_efficiency, device_info.discharge_efficiency) + max(max_charge_kwh, max_discharge_kwh)
    forced_charge_energy = _energy_until_soc(
        next_action_force_battery_charge_rate * theta_hrs,
        (capacity_kwh * next_action_charge_cut_off_soc_percent / 100 - initial_battery) / device_info.charge_efficiency,
        big_m, constraints,
    )
    forced_discharge_energy = _energy_until_soc(
        next_action_force_battery_discharge_rate * theta_hrs,
        (initial_battery - capacity_kwh * next_action_discharge_cut_off_soc_percent / 100) * device_info.discharge_efficiency,
        big_m, constraints,
    )
    battery_level_eop = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="battery_level_eop")

    period_limits = [system_limits(device_info, duration) for duration in durations]
    tariff_zone = ZoneInfo(config.timezone)
    dict_df_results = {scenario.name: pd.DataFrame(index=range(num_time_periods), columns=plan_columns) for scenario in scenarios}
    for t, (starts_at, ends_at) in enumerate(periods):
        local = starts_at.astimezone(tariff_zone)
        import_rules, export_rules = tariff_rules_for_timestamp(config.tariffs, local)
        import_rule = tariff_rule(import_rules, local)
        limit_battery_export_rate_to_grid_kw = tariff_rule_rate(config.energy_plan.limit_battery_export_rate_to_grid_kw, local, 9999)
        minimum_battery_kwh = tariff_rule(config.energy_plan.minimum_battery_kwh, local).rate
        maximum_battery_kwh = min(capacity_kwh, tariff_rule(config.energy_plan.maximum_battery_kwh, local).rate)
        export_rate = tariff_rule(export_rules, local).rate
        for scenario in scenarios:
            forecast_point = scenario.forecast_data.sum(starts_at, ends_at)
            dict_df_results[scenario.name].loc[t] = {"starts_at": starts_at, "ends_at": ends_at, "tariff_name": cast(str, import_rule.name), "import_rate": import_rule.rate, "export_rate": export_rate, "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw, "minimum_battery_kwh": minimum_battery_kwh, "maximum_battery_kwh": maximum_battery_kwh, "load_kwh": forecast_point.load_kwh, "solar_kwh": forecast_point.solar_kwh}
    inputs = {column: np.column_stack([df[column].to_numpy(dtype=float) for df in dict_df_results.values()])
              for column in plan_columns if column not in ("starts_at", "ends_at", "tariff_name")}
    hours = np.broadcast_to(np.array(durations)[:, None], solar_to_load.shape)
    max_charge = np.broadcast_to(np.array([limit.max_charge_kwh for limit in period_limits])[:, None], solar_to_load.shape)
    max_discharge = np.broadcast_to(np.array([limit.max_discharge_kwh for limit in period_limits])[:, None], solar_to_load.shape)
    inverter_flows_dc_to_ac = solar_to_load + solar_to_grid + battery_to_load + battery_to_grid
    inverter_flows_ac_to_dc = grid_to_battery + grid_to_battery_loss
    battery_charge_input = solar_to_battery + solar_to_battery_loss + inverter_flows_ac_to_dc
    battery_discharge_output = battery_to_load + battery_to_grid
    battery_level_bop = (cp.vstack([np.full((1, scenario_count), initial_battery), battery_level_eop[:-1, :]])
                         if num_time_periods > 1 else np.full((1, scenario_count), initial_battery))
    constraints += [
        battery_charging[0, :] >= forcing[0], battery_charging[0, :] <= 1 - forcing[1],
        grid_to_battery[0, :] <= max_charge[0, :] * forcing[0],
        battery_to_grid[0, :] <= max_discharge[0, :] * forcing[1],
        battery_charge_input[0, :] >= forced_charge_energy,
        battery_charge_input[0, :] <= forced_charge_energy + max_charge[0, :] * (1 - forcing[0]),
        battery_discharge_output[0, :] >= forced_discharge_energy,
        battery_discharge_output[0, :] <= forced_discharge_energy + max_discharge[0, :] * (1 - forcing[1]),
        solar_to_load + solar_to_battery + solar_to_battery_loss + solar_to_grid + solar_clipped == inputs["solar_kwh"],
        solar_to_load + battery_to_load + grid_to_load == inputs["load_kwh"],
        battery_charge_input <= cp.multiply(max_charge, battery_charging),
        battery_discharge_output <= cp.multiply(max_discharge, 1 - battery_charging),
        inverter_flows_dc_to_ac + inverter_flows_ac_to_dc <= device_info.inverter_max_active_power_kw * hours,
        inverter_flows_ac_to_dc <= device_info.inverter_max_absorption_power_kw * hours,
        battery_to_grid <= cp.multiply(inputs["limit_battery_export_rate_to_grid_kw"], hours),
        battery_level_eop == battery_level_bop + solar_to_battery + grid_to_battery - battery_to_load - battery_to_grid - battery_to_ac_loss,
        inputs["minimum_battery_kwh"] <= battery_level_eop,
        battery_level_eop <= inputs["maximum_battery_kwh"],
    ]
    net_cost = (cp.multiply(inputs["import_rate"], grid_to_load + inverter_flows_ac_to_dc)
                - cp.multiply(inputs["export_rate"], solar_to_grid + battery_to_grid))
    net_cost_flows = [net_cost[:, s] for s in range(scenario_count)]
    battery_levels = [battery_level_eop[:, s] for s in range(scenario_count)]

    # #### Optimise Model

    score = score_simulation(net_cost_flows, battery_levels, scenarios, device_info, config, constraints,
                             projection_starts_at, durations)
    problem = cp.Problem(cp.Maximize(score), constraints)

    steps = [(end - start).total_seconds() / 60 for start, end in periods]
    summary = ", ".join(f"{minutes:g}*{sum(1 for _ in group)}" for minutes, group in groupby(steps))
    LOGGER.info("plan_model projection: periods=%d scenarios=%d time_steps=[%s]",
                len(periods), len(scenarios), summary)
    metrics = problem.size_metrics
    booleans = sum(variable.size for variable in problem.variables() if variable.attributes["boolean"])
    LOGGER.info("plan_model complexity: variables=%d booleans=%d constraints=%d parameters=%d",
                metrics.num_scalar_variables, booleans,
                metrics.num_scalar_eq_constr + metrics.num_scalar_leq_constr,
                sum(parameter.size for parameter in problem.parameters()))
    problem.solve(solver=cp.HIGHS, warm_start=False)
    print(f"plan_model solve: status={problem.status} compile_seconds={problem.compilation_time:.3f} "
          f"solver_seconds={problem.solver_stats.solve_time or 0.:.3f} objective={problem.value}", flush=True)
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

        battery_end_values = battery_levels[s].value
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



def _projection_intervals(config: Config, starts_at: datetime) -> list[tuple[datetime, datetime]]:
    """Align configured steps from the preceding UTC hour; keep the first intact."""
    s = config.energy_plan
    timesteps = s.projection_resolution_mins
    if s.num_projection_hours <= 0 or not timesteps or any(minutes <= 0 for minutes in timesteps):
        raise ValueError("projection duration and resolution must be greater than zero")
    cursor = utc_instant(starts_at)
    projection_end = cursor + timedelta(hours=s.num_projection_hours)
    boundary = cursor.replace(minute=0, second=0, microsecond=0)
    first_end = min(cursor + timedelta(minutes=timesteps[0]), projection_end)
    periods = [(cursor, first_end)]
    cursor = first_end
    for minutes in timesteps:
        boundary += timedelta(minutes=minutes)
        if cursor == projection_end:
            break
        if boundary <= cursor:
            continue  # Drop boundaries overtaken by the intact first action.
        next_end = min(boundary, projection_end)
        periods.append((cursor, next_end))
        cursor = next_end
    if cursor < projection_end:
        periods.append((cursor, projection_end))
    return periods


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
    """Maximise protection reward minus electricity cost, averaged over scenarios.

    R is stored kWh measured from zero; X is protection_preference (0-100).
    The maximum reward is X/100 * R * average import rate * horizon hours/24.
    Price is time-weighted over the next 24 elapsed hours, independent of load.
    Each scenario earns that reward times its average diminishing-return score
    divided by 100. Cost always matters, including at X=100. No reference solves
    or min/max normalisation are needed.
    """
    projection_starts_at = utc_instant(projection_starts_at)
    settings = config.energy_plan
    projection_hours = sum(durations) if durations is not None else settings.num_projection_hours
    protection_value = (settings.protection_preference / 100 * settings.preferred_reserve_kwh
                        * _reference_import_rate(config, projection_starts_at, projection_starts_at + timedelta(hours=24))
                        * projection_hours / 24)
    scores = []
    for scenario, costs, levels in zip(scenarios, net_cost_flows, battery_levels):
        cost, protection = score_scenario(costs, levels, device, config, constraints, durations)
        scores.append(scenario.probability * (-cost + protection_value * protection / 100))

    return cp.sum(scores) / sum(s.probability for s in scenarios)


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


def _energy_until_soc(requested: cp.Expression, headroom: cp.Expression, big_m: float | cp.Expression, constraints: list[cp.Constraint]) -> cp.Variable:
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
