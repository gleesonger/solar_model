"""Experimental discrete-action planner with an interpolated battery value grid."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import math
from time import perf_counter
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pandas as pd

from solar_model.config import Config, DPRefinementConfig, EnergyPlanConfig
from solar_model.logging_setup import LOGGER
from solar_model.planning.types import DeviceInfo, DeviceState, NextAction, NoFeasiblePlanError, OperatingMode, ScenarioData, SimulationResult
from solar_model.planning.system import allocate_energy_routes, system_limits
from solar_model.tariffs import tariff_rule, tariff_rule_rate, tariff_rules_for_timestamp
from solar_model.time_utils import utc_instant


class Period(NamedTuple):
    """Scenario inputs with soft battery bounds and UTC epoch-second endpoints."""
    starts_at: float
    ends_at: float
    solar: float
    load: float
    import_rate: float
    export_rate: float
    tariff_name: str
    minimum: float
    maximum: float
    export_limit: float
    hours: float
    protection_weight: float
    preferred_reserve_kwh: float


class DeviceParameters(NamedTuple):
    inverter_ac_to_dc_efficiency: float
    inverter_dc_to_ac_efficiency: float
    battery_charge_efficiency: float
    battery_discharge_efficiency: float
    inverter_max_active_power_kw: float
    inverter_max_absorption_power_kw: float
    inverter_battery_charge_power_kw: float
    inverter_battery_discharge_power_kw: float
    grid_max_import_power_kw: float = float("inf")


class Step(NamedTuple):
    solar_used: float
    charge: float
    grid_charge_ac: float
    discharge: float
    battery_end: float
    grid_import: float
    grid_export: float
    net_cost: float
    protection_score: float
    limit_score: float
    mode: OperatingMode

    @property
    def score(self) -> float:
        return step_score(self)


class Action(NamedTuple):
    mode: OperatingMode
    rate_kw: float


def simulate_energy_plan(device_info: DeviceInfo, device_state: DeviceState, scenarios: list[ScenarioData], config: Config, projection_starts_at: datetime, *, grid_step_percent: float | None = None, power_divisions: int | None = None, battery_bounds: list[tuple[float, float]] | dict[str, list[tuple[float, float]]] | None = None) -> SimulationResult:
    """Run configured coarse-to-fine DP; explicit resolution/bounds select one pass."""
    began_at = perf_counter()
    compilation_seconds = 0.
    try:
        settings = config.energy_plan.optimiser.dynamic_programming
        if not settings.refinement_stages or settings.refinement_stages[0].band_percent is not None:
            raise ValueError("DP stages must begin with a full-range stage")
        initial_stage = settings.refinement_stages[0]
        single_pass = grid_step_percent is not None or power_divisions is not None or battery_bounds is not None
        if single_pass:
            stages = (DPRefinementConfig(initial_stage.grid_step_percent if grid_step_percent is None else grid_step_percent, initial_stage.power_divisions if power_divisions is None else power_divisions, None),)
        else:
            stages = settings.refinement_stages
        LOGGER.info("DP Model Start - num_projection_hours=%d num_time_steps=%d refinement_stages=%d use_numba=%s", config.energy_plan.num_projection_hours, len(projection_intervals(config, projection_starts_at)), len(stages), settings.use_numba)
        if settings.use_numba:
            from numba.core.event import TimingListener, install_listener
        capacity = system_limits(device_info, 1.).capacity_kwh
        result, best_score = None, -math.inf
        for index, stage in enumerate(stages):
            stage_at = began_at if index == 0 else perf_counter()
            bounds = battery_bounds if single_pass else None
            if stage.band_percent is not None:
                bounds = {}
                for scenario in scenarios:
                    energies = [*result.results[scenario.name].battery_end_kwh, *result.lookahead_results[scenario.name].battery_end_kwh]
                    bounds[scenario.name] = [(max(0., energy / capacity - stage.band_percent / 100), min(1., energy / capacity + stage.band_percent / 100)) for energy in energies]
            timer = TimingListener() if settings.use_numba else None
            try:
                with install_listener("numba:compile", timer) if settings.use_numba else nullcontext():
                    refined = solve_pass(device_info, device_state, scenarios, config, projection_starts_at, grid_step_percent=stage.grid_step_percent, power_divisions=stage.power_divisions, battery_bounds=bounds)
            except NoFeasiblePlanError:
                if result is None:
                    raise
                LOGGER.warning("DP Refinement Stage #%d infeasible - retaining previous result", index + 1)
                refined = None
            score = -math.inf if refined is None else result_score(refined, scenarios)
            accepted = score > best_score
            if accepted:
                result, best_score = refined, score
            compiled = timer.duration if timer is not None and timer.done else 0.
            elapsed = perf_counter() - stage_at - compiled
            compilation_seconds += compiled
            LOGGER.info("DP Refinement Stage #%d complete - compilation_seconds=%.3f projection_seconds=%.3f score=%.9f accepted=%s", index + 1, compiled, elapsed, best_score, accepted)
        elapsed = perf_counter() - began_at
        LOGGER.info("DP Project end - elapsed_seconds=%.3f compile_seconds=%.3f projection_seconds=%.3f score=%.9f", elapsed, compilation_seconds, elapsed - compilation_seconds, best_score)
        return result
    except NoFeasiblePlanError as error:
        LOGGER.warning("DP unable to calculate a plan - %s - elapsed_seconds=%.3f", error, perf_counter() - began_at)
        raise
    except Exception:
        LOGGER.exception("DP Project failed - elapsed_seconds=%.3f", perf_counter() - began_at)
        raise


def solve_pass(device_info: DeviceInfo, device_state: DeviceState, scenarios: list[ScenarioData], config: Config, projection_starts_at: datetime, *, grid_step_percent: float = 2., power_divisions: int = 5, battery_bounds: list[tuple[float, float]] | dict[str, list[tuple[float, float]]] | None = None) -> SimulationResult:
    """Compare shared first commands using independent scenario DP tables."""
    began_at = perf_counter()
    
    names = [s.name for s in scenarios]

    if not scenarios or any(not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError("scenario names must be non-empty and unique")
    if any(not math.isfinite(s.probability) or s.probability < 0 for s in scenarios) or sum(s.probability for s in scenarios) <= 0:
        raise ValueError("scenario probabilities must be finite, non-negative and have a positive total")
    if not math.isfinite(grid_step_percent) or not 0 < grid_step_percent <= 100:
        raise ValueError("grid step must be between 0 and 100 percent")
    
    reserve = config.energy_plan.preferred_reserve_kwh
    if not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("preferred_reserve_kwh must be finite and greater than zero")
    
    initial = float(device_state.battery_kwh)
    capacity = system_limits(device_info, 1.).capacity_kwh
    
    if not math.isfinite(initial) or not 0 <= initial <= capacity:
        raise ValueError("initial battery energy must be within physical capacity")

    validated_at = perf_counter()

    start = utc_instant(projection_starts_at)
    intervals = projection_intervals(config, start)

    visible_end = start + timedelta(hours=config.energy_plan.num_visible_projection_hours)
    num_visible_periods = sum(end <= visible_end for _, end in intervals)
    num_hidden_periods = len(intervals) - num_visible_periods

    total_hours = sum((end - begin).total_seconds() / 3600 for begin, end in intervals)
    protection_reward_value = protection_value(config, start, total_hours)
    terminal_credit_rate = terminal_rate(config, intervals[-1][1]) * device_info.battery_discharge_efficiency * device_info.inverter_dc_to_ac_efficiency
    
    zone = ZoneInfo(config.timezone)
    setup_at = perf_counter()

    scenario_periods = []
    for scenario in scenarios:
        periods = []
        for begin_utc, end_utc in intervals:
            begin_local  = begin_utc.astimezone(zone)
            end_local = end_utc.astimezone(zone)

            imports, exports = tariff_rules_for_timestamp(config.tariffs, begin_local)
            buy, sell = tariff_rule(imports, begin_local), tariff_rule(exports, begin_local)
            minimum = tariff_rule(config.energy_plan.minimum_battery_kwh, begin_local).rate
            maximum = tariff_rule(config.energy_plan.maximum_battery_kwh, begin_local).rate
            export_limit = tariff_rule_rate(config.energy_plan.limit_battery_export_rate_to_grid_kw, begin_local, 9999)
            forecast = scenario.forecast_data.sum(begin_utc, end_utc)
            hours = (end_utc - begin_utc).total_seconds() / 3600
            numbers = (forecast.solar_kwh, forecast.load_kwh, buy.rate, sell.rate, minimum, maximum, export_limit)

            if not all(math.isfinite(v) for v in numbers) or min(forecast.solar_kwh, forecast.load_kwh, export_limit) < 0:
                raise ValueError("planning inputs must be finite; solar, load and export limits must be non-negative")
            if maximum < max(0., minimum):
                raise ValueError("inconsistent battery energy bounds")
            
            periods.append(Period(begin_utc.timestamp(), end_utc.timestamp(), float(forecast.solar_kwh), float(forecast.load_kwh), float(buy.rate), float(sell.rate), buy.name, float(minimum), float(maximum), float(export_limit), hours, protection_reward_value * hours / total_hours / 100, float(reserve)))

        scenario_periods.append(periods)
    periods_at = perf_counter()

    # Include exact schedule boundaries and the measured state alongside the SOC grid.
    grid = {capacity * min(100., i * grid_step_percent) / 100 for i in range(math.ceil(100 / grid_step_percent) + 1)}
    grid.add(initial)
    
    for period in scenario_periods[0]:
        grid.update((min(capacity, max(0., period.minimum)), min(capacity, max(0., period.maximum))))

    grid = sorted(grid)
    if isinstance(battery_bounds, dict) and set(battery_bounds) != set(names):
        raise ValueError("battery bounds must cover exactly the supplied scenario names")
    scenario_grids = []
    for scenario in scenarios:
        bounds = battery_bounds.get(scenario.name) if isinstance(battery_bounds, dict) else battery_bounds
        if bounds is None:
            bounds = [(0., 1.)] * len(intervals)
        elif len(bounds) == num_visible_periods:
            bounds = [*bounds, *[(0., 1.) for _ in range(num_hidden_periods)]]
        if len(bounds) != len(intervals):
            raise ValueError("battery bounds need one pair per visible period or per full-horizon period")
        scenario_grids.append(bounded_grids(grid, capacity, bounds))
    choices = actions(device_info, config.energy_plan, power_divisions)
    
    use_numba = config.energy_plan.optimiser.dynamic_programming.use_numba
    calculation_device = DeviceParameters(*(float(getattr(device_info, field)) for field in DeviceParameters._fields))

    if use_numba:
        backward, forward = get_numba_functions()
        # Convert lists to numba lists
        from numba.typed import List
        calculation_periods = [List(periods) for periods in scenario_periods]
        calculation_grids = [List([List(grid) for grid in grids]) for grids in scenario_grids]
        calculation_choices = List(choices)
    else:
        backward, forward = backward_values, forward_projection
        calculation_periods, calculation_grids, calculation_choices = scenario_periods, scenario_grids, choices

    grid_at = perf_counter()

    tables = [backward(calculation_device, capacity, periods, grids, calculation_choices, terminal_credit_rate) for periods, grids in zip(calculation_periods, calculation_grids)]

    if use_numba:
        tables = [List([List(row) for row in table]) for table in tables]

    backward_at = perf_counter()

    # Select using actual feasible rollouts, rather than interpolated scores alone.
    best_score, best_action, best_paths = -math.inf, None, None
    scoring_seconds = 0.
    for action in choices:
        paths = []
        score = 0.
        for scenario, periods, grids, values in zip(scenarios, calculation_periods, calculation_grids, tables):
            path = forward(calculation_device, capacity, periods, grids, values, calculation_choices, initial, action)
            if path is None:
                break
            paths.append(path)
            scoring_at = perf_counter()
            score += scenario.probability * (sum(step.score for step in path) + terminal_credit_rate * path[-1].battery_end)
            scoring_seconds += perf_counter() - scoring_at
        if len(paths) == len(scenarios) and score > best_score:
            best_score, best_action, best_paths = score, action, paths
    if best_paths is None:
        raise NoFeasiblePlanError("unable to find a feasible energy plan on the dynamic-programming grid")
    forward_at = perf_counter()

    frames = {s.name: result_frame(device_info, periods, path, initial, terminal_credit_rate) for s, periods, path in zip(scenarios, scenario_periods, best_paths)}
    first_ends = [path[0].battery_end for path in best_paths]
    target = None
    if best_action.mode == OperatingMode.CHARGE_PV_FIRST and best_action.rate_kw > 0:
        target = max(first_ends) / capacity * 100
    elif best_action.mode == OperatingMode.DISCHARGE_PV_FIRST and best_action.rate_kw > 0:
        target = min(first_ends) / capacity * 100
    result = SimulationResult(
        next_action=command(best_action, device_info, target),
        results={name: frame.iloc[:num_visible_periods].copy() for name, frame in frames.items()},
        lookahead_results={name: frame.iloc[num_visible_periods:].reset_index(drop=True) for name, frame in frames.items()},
    )
    finished_at = perf_counter()
    LOGGER.debug(
        "dp_model timings: validation_seconds=%.3f setup_seconds=%.3f periods_seconds=%.3f "
        "grid_actions_seconds=%.3f backward_seconds=%.3f forward_seconds=%.3f scoring_seconds=%.3f "
        "reports_seconds=%.3f elapsed_seconds=%.3f",
        validated_at - began_at,
        setup_at - validated_at,
        periods_at - setup_at,
        grid_at - periods_at,
        backward_at - grid_at,
        forward_at - backward_at - scoring_seconds,
        scoring_seconds,
        finished_at - forward_at,
        finished_at - began_at,
    )
    return result


def result_score(result: SimulationResult, scenarios: list[ScenarioData]) -> float:
    """Realised full-horizon score with normalised scenario probabilities."""
    return float(sum(s.probability * (result.results[s.name].total_score.sum() + result.lookahead_results[s.name].total_score.sum()) for s in scenarios) / sum(s.probability for s in scenarios))


def projection_intervals(config: Config, starts_at: datetime) -> list[tuple[datetime, datetime]]:
    """Align the first crossed UTC hour and defer the removed duration to the end."""
    settings = config.energy_plan
    minutes = settings.optimiser.dynamic_programming.projection_resolution_mins
    if settings.num_projection_hours <= 0 or not minutes or any(type(step) is not int or step <= 0 for step in minutes):
        raise ValueError("DP projection duration and timesteps must be positive")
    if sum(minutes) < settings.num_projection_hours * 60:
        raise ValueError("DP projection_resolution_mins must cover num_projection_hours")
    cursor = utc_instant(starts_at)
    end = cursor + timedelta(hours=settings.num_projection_hours)
    visible_end = min(end, cursor + timedelta(hours=settings.num_visible_projection_hours))
    first_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    crossed_hour = False
    deferred = timedelta(0)
    intervals = []
    for step in minutes:
        next_end = min(end - deferred, cursor + timedelta(minutes=step))
        if not crossed_hour and next_end >= first_hour:
            crossed_hour = True
            deferred = next_end - first_hour
            next_end = first_hour
        if cursor < visible_end < next_end:
            intervals.append((cursor, visible_end))
            cursor = visible_end
        intervals.append((cursor, next_end))
        cursor = next_end
        if cursor == end - deferred:
            break
    if cursor < visible_end < end:
        intervals.append((cursor, visible_end))
        cursor = visible_end
    if cursor < end:
        intervals.append((cursor, end))
    return intervals



def protection_value(config: Config, starts_at: datetime, projection_hours: float) -> float:
    settings = config.energy_plan
    return settings.protection_preference / 100 * settings.preferred_reserve_kwh * reference_import_rate(config, starts_at, starts_at + timedelta(hours=24)) * projection_hours / 24


def terminal_rate(config: Config, ends_at: datetime) -> float:
    """Mean hourly import/export midpoint over the 12 hours after the model end."""
    end = utc_instant(ends_at)
    zone = ZoneInfo(config.timezone)
    rates = []
    for hour in range(12):
        local = (end + timedelta(hours=hour)).astimezone(zone)
        imports, exports = tariff_rules_for_timestamp(config.tariffs, local)
        rates.append((tariff_rule(imports, local).rate + tariff_rule(exports, local).rate) / 2)
    return sum(rates) / len(rates)


def actions(device: DeviceInfo, settings: EnergyPlanConfig, power_divisions: int = 5) -> list[Action]:
    """Three policies, including zero and positive forced power variations."""
    if type(power_divisions) is not int or power_divisions <= 0:
        raise ValueError("power divisions must be a positive integer")
    choices = [Action(OperatingMode.SELF_CONSUMPTION, 0.), Action(OperatingMode.CHARGE_PV_FIRST, 0.), Action(OperatingMode.DISCHARGE_PV_FIRST, 0.)]
    limits = system_limits(device, 1.)
    for mode, minimum, maximum in (
        (OperatingMode.CHARGE_PV_FIRST, settings.min_forced_charge_power_kw, min(settings.max_forced_charge_power_kw, limits.max_charge_kwh)),
        (OperatingMode.DISCHARGE_PV_FIRST, settings.min_forced_discharge_power_kw, min(settings.max_forced_discharge_power_kw, limits.max_discharge_kwh)),
    ):
        if maximum >= minimum:
            for i in range(1, power_divisions + 1):
                choice = Action(mode, minimum + (maximum - minimum) * (i / power_divisions))
                if choice not in choices:
                    choices.append(choice)
    return choices


def bounded_grids(grid: list[float], capacity: float, bounds: list[tuple[float, float]]) -> list[list[float]]:
    """Restrict period-end search nodes without changing action profiles."""
    grids = [grid]
    for lower, upper in bounds:
        if not math.isfinite(lower) or not math.isfinite(upper) or not 0 <= lower <= upper <= 1:
            raise ValueError("battery bounds must be finite SOC fractions with 0 <= lower <= upper <= 1")
        minimum, maximum = lower * capacity, upper * capacity
        nodes = {energy for energy in grid if minimum <= energy <= maximum}
        nodes.update((minimum, maximum))
        grids.append(sorted(nodes))
    return grids


@lru_cache(maxsize=1)
def get_numba_functions():
    """Import Numba locally and cache lazy compiled versions of the same functions."""
    try:
        from numba import njit
        from numba.extending import register_jitable
    except ModuleNotFoundError as error:
        if error.name != "numba":
            raise
        raise RuntimeError('Numba requires installation with: pip install -e ".[numba]"') from error
    for helper in (simulate_system_single_time_period, interpolate, battery_protection_score, battery_limit_score, step_score):
        register_jitable(helper)
    return njit(cache=False)(backward_values), njit(cache=False)(forward_projection)


def backward_values(device: DeviceParameters, capacity: float, periods: list[Period], grids: list[list[float]], choices: list[Action], terminal_rate: float) -> list[list[float]]:
    values = [[terminal_rate * battery for battery in grids[-1]]]
    for t in range(len(periods) - 1, -1, -1):
        period = periods[t]
        next_grid, next_values = grids[t + 1], values[-1]
        current = []
        for battery in grids[t]:
            best = -math.inf
            charge_skip_from = discharge_skip_from = math.inf
            for action in choices:
                mode, rate_kw = action
                if (mode == OperatingMode.CHARGE_PV_FIRST and rate_kw >= charge_skip_from) or (mode == OperatingMode.DISCHARGE_PV_FIRST and rate_kw >= discharge_skip_from):
                    continue
                step = simulate_system_single_time_period(device, capacity, period, battery, action)
                if step is not None:
                    # Higher rates cannot improve a capped flow or return to the search corridor.
                    if mode == OperatingMode.CHARGE_PV_FIRST and (step.charge < rate_kw * period.hours or step.battery_end > next_grid[-1] + 1e-9):
                        charge_skip_from = min(charge_skip_from, rate_kw)
                    elif mode == OperatingMode.DISCHARGE_PV_FIRST and (step.discharge < rate_kw * period.hours or step.battery_end < next_grid[0] - 1e-9):
                        discharge_skip_from = min(discharge_skip_from, rate_kw)
                    best = max(best, step_score(step) + interpolate(next_grid, next_values, step.battery_end))
            current.append(best)
        values.append(current)
    return values[::-1]


def forward_projection(device: DeviceParameters, capacity: float, periods: list[Period], grids: list[list[float]], values: list[list[float]], choices: list[Action], initial: float, first_action: Action) -> list[Step] | None:
    """Re-evaluate the policy at exact states, including the shared first action."""
    battery = initial
    path = []
    for t, period in enumerate(periods):
        next_grid, next_values = grids[t + 1], values[t + 1]
        best_value, best_step = -math.inf, None
        charge_skip_from = discharge_skip_from = math.inf
        for i in range(1 if t == 0 else len(choices)):
            action = first_action if t == 0 else choices[i]
            mode, rate_kw = action
            if (mode == OperatingMode.CHARGE_PV_FIRST and rate_kw >= charge_skip_from) or (mode == OperatingMode.DISCHARGE_PV_FIRST and rate_kw >= discharge_skip_from):
                continue
            step = simulate_system_single_time_period(device, capacity, period, battery, action)
            if step is not None:
                # Keep the same pruning rule as the backward pass, at the actual battery level.
                if mode == OperatingMode.CHARGE_PV_FIRST and (step.charge < rate_kw * period.hours or step.battery_end > next_grid[-1] + 1e-9):
                    charge_skip_from = min(charge_skip_from, rate_kw)
                elif mode == OperatingMode.DISCHARGE_PV_FIRST and (step.discharge < rate_kw * period.hours or step.battery_end < next_grid[0] - 1e-9):
                    discharge_skip_from = min(discharge_skip_from, rate_kw)
                value = step_score(step) + interpolate(next_grid, next_values, step.battery_end)
                if value > best_value:
                    best_value, best_step = value, step
        if best_step is None:
            return None
        path.append(best_step)
        battery = best_step.battery_end
    return path


def result_frame(device: DeviceInfo, periods: list[Period], path: list[Step], initial: float, terminal_rate: float) -> pd.DataFrame:
    frame = pd.DataFrame([
        {
            "starts_at": datetime.fromtimestamp(p.starts_at, timezone.utc),
            "ends_at": datetime.fromtimestamp(p.ends_at, timezone.utc),
            "tariff_name": p.tariff_name,
            "import_rate": p.import_rate,
            "export_rate": p.export_rate,
            "minimum_battery_kwh": p.minimum,
            "maximum_battery_kwh": p.maximum,
            "limit_battery_export_rate_to_grid_kw": p.export_limit,
            "solar_kwh": p.solar,
            "load_kwh": p.load,
            "battery_end_kwh": step.battery_end,
            "plan_action": OperatingMode(step.mode).label,
            "grid_import_kwh": step.grid_import,
            "grid_export_kwh": step.grid_export,
            "battery_charging": float(step.charge > 0),
            "import_cost": step.grid_import * p.import_rate,
            "export_revenue": step.grid_export * p.export_rate,
            "net_cost": step.net_cost,
            "cost_score": -step.net_cost,
            "protection_score": step.protection_score,
            "limit_score": step.limit_score,
        }
        for p, step in zip(periods, path)
    ])
    frame["battery_start_kwh"] = [initial, *[step.battery_end for step in path[:-1]]]
    
    routes = allocate_energy_routes(frame.solar_kwh, frame.load_kwh, [s.solar_used for s in path], [s.charge for s in path], [s.grid_charge_ac for s in path], [s.discharge for s in path], device, [p.hours for p in periods])
    
    for name, amounts in routes.items():
        frame[f"{name}_kwh"] = amounts

    frame["solar_to_inverter_kwh"] = [s.solar_used for s in path]
    frame["battery_to_inverter_kwh"] = [s.discharge for s in path]
    frame["inverter_dc_to_ac_kwh"] = frame.solar_to_load_kwh + frame.solar_to_grid_kwh + frame.battery_to_inverter_kwh
    frame["inverter_ac_to_dc_kwh"] = [s.grid_charge_ac * device.inverter_ac_to_dc_efficiency for s in path]
    frame["terminal_score"] = 0.
    frame.loc[frame.index[-1], "terminal_score"] = terminal_rate * path[-1].battery_end
    frame["total_score"] = frame.terminal_score + [step_score(step) for step in path]
    return frame


def command(action: Action, device: DeviceInfo, target_soc_percent: float | None = None) -> NextAction:
    """Map a policy variation to a Sigenergy mode and both power ceilings."""
    mode, rate = action
    charge_limit = device.inverter_battery_charge_power_kw
    discharge_limit = device.inverter_battery_discharge_power_kw
    if mode == OperatingMode.CHARGE_PV_FIRST:
        charge_limit, discharge_limit = rate, 0.
    elif mode == OperatingMode.DISCHARGE_PV_FIRST:
        discharge_limit = rate
    elif mode != OperatingMode.SELF_CONSUMPTION:
        raise ValueError("unsupported DP command")
    return NextAction(
        mode=mode,
        charge_power_limit_kw=charge_limit,
        discharge_power_limit_kw=discharge_limit,
        target_soc_percent=target_soc_percent,
    )


def simulate_system_single_time_period(device: DeviceParameters, capacity: float, period: Period, battery: float, action: Action) -> Step | None:
    """Simulate physical command response and score the ending battery state."""
    mode, rate = action
    pv, load, hours = period.solar, period.load, period.hours
    eta_ac_dc = device.inverter_ac_to_dc_efficiency
    eta_dc_ac = device.inverter_dc_to_ac_efficiency
    eta_charge = device.battery_charge_efficiency
    eta_discharge = device.battery_discharge_efficiency
    output_ac = device.inverter_max_active_power_kw * hours
    output_dc = output_ac / eta_dc_ac
    absorption_ac = min(output_ac, device.inverter_max_absorption_power_kw * hours)
    charge_limit = device.inverter_battery_charge_power_kw * hours
    discharge_limit = min(device.inverter_battery_discharge_power_kw * hours, output_dc)
    charge = discharge = grid_charge_ac = 0.
    pv_load_dc = min(pv, load / eta_dc_ac, output_dc)
    charge_room = max(0., capacity - battery) / eta_charge

    if mode == OperatingMode.SELF_CONSUMPTION:
        charge = min(pv - pv_load_dc, charge_limit, charge_room)
        discharge = min(load / eta_dc_ac - pv_load_dc, discharge_limit, output_dc - pv_load_dc, battery * eta_discharge)
        used = min(pv, charge + output_dc - discharge)
    elif mode == OperatingMode.CHARGE_PV_FIRST:
        pv_surplus = pv - pv_load_dc
        grid_headroom = max(0., device.grid_max_import_power_kw * hours - max(0., load - pv_load_dc * eta_dc_ac))
        charge = min(rate * hours, charge_room, charge_limit, pv_surplus + absorption_ac * eta_ac_dc, pv_surplus + grid_headroom * eta_ac_dc)
        grid_charge_ac = max(0., charge - pv_surplus) / eta_ac_dc
        used = min(pv, charge - grid_charge_ac * eta_ac_dc + output_dc)
    elif mode == OperatingMode.DISCHARGE_PV_FIRST:
        if rate == 0:
            charge = min(pv - pv_load_dc, charge_limit, charge_room)
            used = min(pv, charge + output_dc)
        else:
            used = min(pv, output_dc)
            ceiling = min(discharge_limit, output_dc - used)
            allowance_ac = max(0., load - used * eta_dc_ac) + period.export_limit * hours
            if ceiling * eta_dc_ac > allowance_ac + 1e-9 and rate * hours * eta_dc_ac > allowance_ac + 1e-9:
                return None
            discharge = min(rate * hours, battery * eta_discharge, ceiling)
    else:
        raise ValueError("unsupported DP command")

    battery_end = min(capacity, max(0., battery + eta_charge * charge - discharge / eta_discharge))
    solar_charge_dc = charge - grid_charge_ac * eta_ac_dc
    exchange = (used - solar_charge_dc + discharge) * eta_dc_ac - grid_charge_ac - load
    grid_export, grid_import = max(0., exchange), max(0., -exchange)
    if grid_import > device.grid_max_import_power_kw * hours + 1e-9:
        return None
    if min(discharge * eta_dc_ac, grid_export) > period.export_limit * hours + 1e-9:
        return None
    protection = battery_protection_score(battery_end, period.preferred_reserve_kwh)
    net_cost = grid_import * period.import_rate - grid_export * period.export_rate
    limit_score = battery_limit_score(battery_end, period.minimum, period.maximum, capacity, hours)
    return Step(used, charge, grid_charge_ac, discharge, battery_end, grid_import, grid_export, net_cost, period.protection_weight * min(100., max(0., protection)), limit_score, mode)


def step_score(step: Step) -> float:
    """Period objective shared by search, realised path scoring and Step.score."""
    return -step.net_cost + step.protection_score + step.limit_score


def battery_limit_score(energy: float, minimum: float, maximum: float, capacity: float, hours: float) -> float:
    """Squared-ratio penalty, half of -100 per hour at a five-SOC-point breach."""
    breach = max(minimum - energy, energy - maximum, 0.)
    if breach == 0:
        return 0.
    ratio = capacity * .05 / breach
    return -100 / (1 + ratio * ratio) * hours


def interpolate(x_values: list[float], y_values: list[float], x: float) -> float:
    left, right = 0, len(x_values)
    while left < right:
        middle = (left + right) // 2
        if x_values[middle] < x:
            left = middle + 1
        else:
            right = middle
    right = left
    if right == len(x_values):
        return y_values[-1] if x <= x_values[-1] + 1e-9 else -math.inf
    right_x = x_values[right]
    if abs(right_x - x) < 1e-9:
        return y_values[right]
    if right == 0:
        return -math.inf
    left_y = y_values[right - 1]
    if not math.isfinite(left_y):
        return -math.inf
    right_y = y_values[right]
    if not math.isfinite(right_y):
        return -math.inf
    left_x = x_values[right - 1]
    fraction = (x - left_x) / (right_x - left_x)
    return left_y * (1 - fraction) + right_y * fraction


def battery_protection_score(energy: float, reserve: float) -> float:
    """Agreed reserve utility: 0 at empty, 70 at reserve, then diminishing returns."""
    if not math.isfinite(energy) or energy < 0 or not math.isfinite(reserve) or reserve <= 0:
        raise ValueError("stored energy must be finite and non-negative; reserve must be finite and positive")
    if energy <= reserve:
        return 100 * (1 - 0.3 ** (energy / reserve))
    return 70 + 30 * (energy - reserve) / (energy + reserve)


def reference_import_rate(config: Config, starts_at: datetime, ends_at: datetime) -> float:
    """Approximate the reference price using one tariff sample per elapsed hour."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (starts_at, ends_at)):
        raise ValueError("reference tariff range must be timezone-aware")
    cursor, end = utc_instant(starts_at), utc_instant(ends_at)
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
