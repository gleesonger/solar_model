"""Nine-condition stochastic DP using the existing battery physics and objective.

Commands precede interval outcomes. Future condition identities are assumed
observable at interval ends; this approximation includes unobservable night solar.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
import math

import pandas as pd

from solar_model.config import Config
from solar_model.logging_setup import LOGGER
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.markov_transitions import transition
from solar_model.planning.types import DeviceInfo, NoFeasiblePlanError, OperatingMode, ScenarioData, SimulationResult


LEVELS = ("low", "med", "high")
LAYERS = tuple((load, solar) for load in LEVELS for solar in LEVELS)
NAMES = tuple("med" if load == solar == "med" else f"{load}_{solar}" for load, solar in LAYERS)
INITIAL_LAYER = 4


def solve_prepared(
    device: DeviceInfo,
    capacity: float,
    initial: float,
    scenarios: list[ScenarioData],
    scenario_periods: list[list[dp.Period]],
    grid: list[float],
    config: Config,
    power_divisions: int,
    terminal_rate: float,
    visible_count: int,
) -> SimulationResult:
    """Reuse validated interval inputs; discard old path weights and timing stress."""
    if set(s.name for s in scenarios) != set(NAMES):
        raise ValueError("Markov DP requires all nine named low/med/high solar-load combinations")
    settings = config.energy_plan.optimiser
    dimensions = config.energy_plan.scenario_multiplier
    base = scenario_periods[next(i for i, s in enumerate(scenarios) if s.name == "med")]
    if dimensions.solar.med <= 0 or dimensions.house_load.med <= 0:
        raise ValueError("Markov DP requires positive medium multipliers")
    periods = []
    transitions = []
    for period in base:
        periods.append([
            period._replace(
                solar=period.solar * getattr(dimensions.solar, solar) / dimensions.solar.med,
                load=period.load * getattr(dimensions.house_load, load) / dimensions.house_load.med,
            )
            for load, solar in LAYERS
        ])
        transitions.append(joint_transition(period.hours, settings.transition_matrix.solar, settings.transition_matrix.house_load, settings.transition_hours))
    choices = dp.actions(device, config.energy_plan, power_divisions)
    numerical_device = dp.DeviceParameters(*(float(getattr(device, field)) for field in dp.DeviceParameters._fields))
    backward, forward = backward_values, forward_distribution
    if settings.compile_model_to_native_code:
        from numba.typed import List
        backward, forward = compiled_functions()
        numerical_periods = List([List(row) for row in periods])
        numerical_transitions = List([List([List(row) for row in matrix]) for matrix in transitions])
        numerical_grid, numerical_choices = List(grid), List(choices)
    else:
        numerical_periods, numerical_transitions = periods, transitions
        numerical_grid, numerical_choices = grid, choices

    values, policy = backward(numerical_device, capacity, numerical_periods, numerical_grid, numerical_choices, numerical_transitions, terminal_rate)
    initial_index = grid.index(initial)
    expected_score = values[0][INITIAL_LAYER][initial_index]
    if not math.isfinite(expected_score):
        raise NoFeasiblePlanError("no Markov policy is feasible across all positive-probability outcomes")
    first_action = choices[policy[0][INITIAL_LAYER][initial_index]]
    if settings.compile_model_to_native_code:
        numerical_policy = List([List([List(row) for row in time]) for time in policy])
    else:
        numerical_policy = policy
    totals = forward(numerical_device, capacity, numerical_periods, numerical_grid, numerical_choices, numerical_transitions, numerical_policy, initial_index, INITIAL_LAYER)
    expected = expectation_frame(base, totals, terminal_rate)
    if not math.isclose(float(expected.total_score.sum()), expected_score, abs_tol=1e-7, rel_tol=1e-9):
        raise RuntimeError("Markov forward policy score disagrees with backward value")

    # These are fixed-condition stress illustrations, not exhaustive future paths.
    frames = {}
    for layer, name in enumerate(NAMES):
        battery, path = initial, []
        for t in range(len(periods)):
            action = first_action
            if t:
                scores, indices = best_choices(numerical_device, capacity, periods[t], grid, choices, transitions[t], values[t + 1], battery)
                if indices[layer] < 0:
                    raise NoFeasiblePlanError(f"Markov stress path {name} has no feasible exact-state continuation")
                action = choices[indices[layer]]
            step = dp.simulate_system_single_time_period(numerical_device, capacity, periods[t][layer], battery, action)
            if step is None:
                raise NoFeasiblePlanError(f"Markov stress path {name} breaches a physical limit")
            path.append(step)
            battery = step.battery_end
        frames[name] = dp.result_frame(device, [row[layer] for row in periods], path, initial, terminal_rate)
    first_ends = [float(frame.battery_end_kwh.iloc[0]) for frame in frames.values()]
    target = None
    if first_action.rate_kw > 0:
        if first_action.mode == OperatingMode.CHARGE_PV_FIRST:
            target = max(first_ends) / capacity * 100
        elif first_action.mode == OperatingMode.DISCHARGE_PV_FIRST:
            target = min(first_ends) / capacity * 100
    LOGGER.info("Markov DP - layers=9 transition_hours=%g expected_score=%.9f; detail paths are stress illustrations", settings.transition_hours, expected_score)
    return SimulationResult(
        next_action=dp.command(first_action, device, target),
        results={name: frame.iloc[:visible_count].copy() for name, frame in frames.items()},
        lookahead_results={name: frame.iloc[visible_count:].reset_index(drop=True) for name, frame in frames.items()},
        expected_score=expected_score,
        expected_results=expected.iloc[:visible_count].copy(),
        expected_lookahead_results=expected.iloc[visible_count:].reset_index(drop=True),
    )


def joint_transition(hours: float, solar_probabilities: tuple[tuple[float, ...], ...], load_probabilities: tuple[tuple[float, ...], ...], reference_hours: float) -> list[list[float]]:
    solar_matrix = transition(hours, solar_probabilities, reference_hours)
    load_matrix = transition(hours, load_probabilities, reference_hours)
    return [[load_matrix[load][next_load] * solar_matrix[solar][next_solar] for next_load in range(3) for next_solar in range(3)] for load in range(3) for solar in range(3)]


@lru_cache(maxsize=1)
def compiled_functions() -> tuple[Callable[..., tuple[list[list[list[float]]], list[list[list[int]]]]], Callable[..., list[list[float]]]]:
    """Compile the original numerical functions lazily, without warm-up."""
    try:
        from numba import njit
        from numba.extending import register_jitable
    except ModuleNotFoundError as error:
        raise RuntimeError('Numba requires installation with: pip install -e ".[numba]"') from error
    for helper in (dp.simulate_system_single_time_period, dp.interpolate, dp.battery_protection_score, dp.battery_limit_score, dp.step_score, best_choices, bracket):
        register_jitable(helper)
    return njit(cache=True)(backward_values), njit(cache=True)(forward_distribution)


def backward_values(
    device: dp.DeviceParameters,
    capacity: float,
    periods: list[list[dp.Period]],
    grid: list[float],
    choices: list[dp.Action],
    transitions: list[list[list[float]]],
    terminal_rate: float,
) -> tuple[list[list[list[float]]], list[list[list[int]]]]:
    """Average outcomes before choosing commands, separately at every source node."""
    count = len(periods[0])
    values = [[[terminal_rate * battery for battery in grid] for _ in range(count)]]
    policies = []
    for t in range(len(periods) - 1, -1, -1):
        current = [[-math.inf for _ in grid] for _ in range(count)]
        selected = [[-1 for _ in grid] for _ in range(count)]
        for b in range(len(grid)):
            scores, indices = best_choices(device, capacity, periods[t], grid, choices, transitions[t], values[-1], grid[b])
            for source in range(count):
                current[source][b] = scores[source]
                selected[source][b] = indices[source]
        values.append(current)
        policies.append(selected)
    return values[::-1], policies[::-1]


def best_choices(
    device: dp.DeviceParameters,
    capacity: float,
    periods: list[dp.Period],
    grid: list[float],
    choices: list[dp.Action],
    transition: list[list[float]],
    next_values: list[list[float]],
    battery: float,
) -> tuple[list[float], list[int]]:
    """Reuse each physical outcome across sources; never prune from one outcome."""
    count = len(periods)
    best = [-math.inf for _ in periods]
    selected = [-1 for _ in periods]
    for a in range(len(choices)):
        outcomes = [-math.inf for _ in periods]
        for destination in range(count):
            step = dp.simulate_system_single_time_period(device, capacity, periods[destination], battery, choices[a])
            if step is not None:
                continuation = dp.interpolate(grid, next_values[destination], step.battery_end)
                outcomes[destination] = dp.step_score(step) + continuation
        for source in range(count):
            score = 0.
            for destination in range(count):
                probability = transition[source][destination]
                if probability > 0:
                    if not math.isfinite(outcomes[destination]):
                        score = -math.inf
                        break
                    score += probability * outcomes[destination]
            if score > best[source]:
                best[source], selected[source] = score, a
    return best, selected


def bracket(grid: list[float], energy: float) -> tuple[int, int, float]:
    """The same endpoint tolerance and linear weights as DP value interpolation."""
    left, right = 0, len(grid)
    while left < right:
        middle = (left + right) // 2
        if grid[middle] < energy:
            left = middle + 1
        else:
            right = middle
    if left == len(grid):
        return left - 1, left - 1, 0.
    if abs(grid[left] - energy) < 1e-9 or left == 0:
        return left, left, 0.
    return left - 1, left, (energy - grid[left - 1]) / (grid[left] - grid[left - 1])


def forward_distribution(
    device: dp.DeviceParameters,
    capacity: float,
    periods: list[list[dp.Period]],
    grid: list[float],
    choices: list[dp.Action],
    transitions: list[list[list[float]]],
    policy: list[list[list[int]]],
    initial_index: int,
    initial_layer: int,
) -> list[list[float]]:
    """Evaluate the grid policy by probability propagation, not fixed-path weights.

    Split ending mass between adjacent battery nodes, exactly matching the
    discretised backward expectation. This is not a continuous-state rollout.
    """
    count = len(periods[0])
    mass = [[0. for _ in grid] for _ in range(count)]
    mass[initial_layer][initial_index] = 1.
    totals = []
    for t in range(len(periods)):
        following = [[0. for _ in grid] for _ in range(count)]
        # solar, load, opening/closing battery, import/export, cost, protection,
        # limit, below-reserve hours, raw protection, minimum/maximum ending energy.
        row = [0. for _ in range(13)]
        row[11], row[12] = math.inf, -math.inf
        for source in range(count):
            for b in range(len(grid)):
                if mass[source][b] == 0:
                    continue
                a = policy[t][source][b]
                if a < 0:
                    raise ValueError("positive policy probability reached an infeasible node")
                for destination in range(count):
                    probability = mass[source][b] * transitions[t][source][destination]
                    if probability == 0:
                        continue
                    period = periods[t][destination]
                    step = dp.simulate_system_single_time_period(device, capacity, period, grid[b], choices[a])
                    if step is None:
                        raise ValueError("selected Markov command has an infeasible supported outcome")
                    lower, upper, fraction = bracket(grid, step.battery_end)
                    following[destination][lower] += probability * (1 - fraction)
                    following[destination][upper] += probability * fraction
                    amounts = (period.solar, period.load, grid[b], step.battery_end, step.grid_import, step.grid_export, step.net_cost, step.protection_score, step.limit_score, period.hours if step.battery_end < period.preferred_reserve_kwh else 0., dp.battery_protection_score(step.battery_end, period.preferred_reserve_kwh))
                    for k in range(len(amounts)):
                        row[k] += probability * amounts[k]
                    row[11] = min(row[11], step.battery_end)
                    row[12] = max(row[12], step.battery_end)
        total_mass = 0.
        for weights in following:
            total_mass += sum(weights)
        if abs(total_mass - 1.) > 1e-9:
            raise ValueError("Markov policy probability mass is not conserved")
        totals.append(row)
        mass = following
    return totals


def expectation_frame(periods: list[dp.Period], totals: list[list[float]], terminal_rate: float) -> pd.DataFrame:
    """Expose expectations separately; no reconstructed routes or fictitious action."""
    columns = ("solar_kwh", "load_kwh", "battery_start_kwh", "battery_end_kwh", "grid_import_kwh", "grid_export_kwh", "net_cost", "protection_score", "limit_score", "below_reserve_hours", "raw_protection", "minimum_outcome_battery_kwh", "maximum_outcome_battery_kwh")
    frame = pd.DataFrame(totals, columns=columns)
    frame["starts_at"] = [datetime.fromtimestamp(period.starts_at, timezone.utc) for period in periods]
    frame["ends_at"] = [datetime.fromtimestamp(period.ends_at, timezone.utc) for period in periods]
    frame["import_rate"] = [period.import_rate for period in periods]
    frame["export_rate"] = [period.export_rate for period in periods]
    frame["import_cost"] = frame.grid_import_kwh * frame.import_rate
    frame["export_revenue"] = frame.grid_export_kwh * frame.export_rate
    frame["cost_score"] = -frame.net_cost
    frame["terminal_score"] = 0.
    frame.loc[frame.index[-1], "terminal_score"] = terminal_rate * frame.battery_end_kwh.iloc[-1]
    frame["total_score"] = frame.cost_score + frame.protection_score + frame.limit_score + frame.terminal_score
    return frame
