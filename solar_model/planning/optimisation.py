from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import cast
import math
import cvxpy as cp
import pandas as pd
from solar_model.config import Config
from solar_model.time_utils import add_elapsed, utc_instant
from solar_model.tariffs import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp

from solar_model.planning.types import DeviceInfo, DeviceState, ScenarioData, NextAction, OperatingMode, SimulationResult
from solar_model.planning.system import system_limits

MIN_FORCE_RATE_KW = 0.1  # Minimum positive command: 100 watts.
PROTECTION_APPROXIMATION_ERROR = 0.05  # Protection score points.


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

    periods = _projection_intervals(config, projection_starts_at)
    num_time_periods, scenario_count = len(periods), len(scenarios)
    durations = [(utc_instant(end) - utc_instant(start)).total_seconds() / 3600 for start, end in periods]
    theta_hrs = durations[0]

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
    battery_level_eop = cp.Variable((num_time_periods, scenario_count), nonneg=True, name="battery_level_eop")

    # Each period has its own duration and energy limits.
    for t, (starts_at, ends_at) in enumerate(periods):
        theta_hrs = durations[t]
        limits = system_limits(device_info, theta_hrs)
        max_charge_kwh, max_discharge_kwh = limits.max_charge_kwh, limits.max_discharge_kwh
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
            battery_level_bop = device_state.battery_kwh if t == 0 else battery_level_eop[t - 1, s]
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
            constraints += [
                battery_level_eop[t, s] == battery_level_bop + solar_to_battery[t, s] + grid_to_battery[t, s] - battery_to_load[t, s] - battery_to_grid[t, s] - battery_to_ac_loss[t, s],
                minimum_battery_kwh <= battery_level_eop[t, s], battery_level_eop[t, s] <= maximum_battery_kwh,
            ]
            battery_levels[s].append(battery_level_eop[t, s])
            net_cost_flows[s].append(import_rate * (grid_to_load[t, s] + grid_to_battery[t, s] + grid_to_battery_loss[t, s]) - export_rate * (solar_to_grid[t, s] + battery_to_grid[t, s]))
            dict_df_results[scenario.name].loc[t] = {"starts_at": starts_at, "ends_at": ends_at, "tariff_name": cast(str, import_rule.name), "import_rate": import_rate, "export_rate": export_rate, "limit_battery_export_rate_to_grid_kw": limit_battery_export_rate_to_grid_kw, "minimum_battery_kwh": minimum_battery_kwh, "maximum_battery_kwh": maximum_battery_kwh, "load_kwh": load_kwh, "solar_kwh": solar_kwh}

    # #### Optimise Model

    score = score_simulation(net_cost_flows, battery_levels, scenarios, device_info, config, constraints, projection_starts_at, durations)
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


def _projection_intervals(config: Config, starts_at: datetime) -> list[tuple[datetime, datetime]]:
    """Short first action, then hourly periods, split at price and limit changes."""
    settings = config.energy_plan
    if settings.num_projection_hours <= 0 or settings.projection_resolution_mins <= 0:
        raise ValueError("projection duration and resolution must be greater than zero")
    start = utc_instant(starts_at)
    end = start + timedelta(hours=settings.num_projection_hours)
    first_end = min(end, start + timedelta(minutes=settings.projection_resolution_mins))
    boundaries = {start, first_end, end}
    cursor = first_end
    while cursor < end:
        cursor = min(end, cursor + timedelta(hours=1))
        boundaries.add(cursor)
    zone = ZoneInfo(config.timezone)
    day = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.astimezone(timezone.utc) < end:
        imports, exports = tariff_rules_for_timestamp(config.tariffs, day)
        rules = (*imports, *exports, *settings.minimum_battery_kwh, *settings.maximum_battery_kwh,
                 *settings.limit_battery_export_rate_to_grid_kw)
        minutes = {0, 1440, *(rule.start_minutes for rule in rules), *(rule.end_minutes for rule in rules)}
        for minute in minutes:
            for fold in (0, 1):
                boundary = (day + timedelta(minutes=minute)).replace(fold=fold).astimezone(timezone.utc)
                if first_end < boundary < end:
                    boundaries.add(boundary)
        day += timedelta(days=1)
    ordered = sorted(boundaries)
    return [(left.astimezone(zone), right.astimezone(zone)) for left, right in zip(ordered, ordered[1:])]


def score_simulation(
        net_cost_flows: list[list[cp.Expression]],  # Scenario x Time
        battery_levels: list[list[cp.Expression]],
        scenarios: list[ScenarioData],
        device: DeviceInfo,
        config: Config,
        constraints: list[cp.Constraint],
        projection_starts_at: datetime,
        durations: list[float] | None = None,
    ) -> cp.Expression:
    """Maximise protection reward minus electricity cost, averaged over scenarios.

    R is stored kWh measured from zero; X is protection_preference (0-100).
    The maximum reward is X/100 * R * average import rate * horizon hours/24.
    Price is time-weighted over the next 24 elapsed hours, independent of load.
    Each scenario earns that reward times its average diminishing-return score
    divided by 100. Cost always matters, including at X=100. No reference solves
    or min/max normalisation are needed.
    """
    settings = config.energy_plan
    protection_value = (settings.protection_preference / 100 * settings.preferred_reserve_kwh
                        * _reference_import_rate(config, projection_starts_at, add_elapsed(projection_starts_at, timedelta(hours=24)))
                        * settings.num_projection_hours / 24)
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
        costs: list[cp.Expression],
        levels: list[cp.Expression],
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
    stored_energy = cp.hstack(levels)
    protection = cp.Variable(len(levels), nonneg=True)
    # Linear bounds approximate diminishing returns: 0 empty, 70 at reserve,
    # approaching 100 as energy increases. Maximisation lifts scores onto the curve.
    constraints.append(protection <= 100)
    constraints.extend(protection <= slope * stored_energy + intercept for slope, intercept in segments)
    durations = durations if durations is not None else [1.] * len(levels)
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
