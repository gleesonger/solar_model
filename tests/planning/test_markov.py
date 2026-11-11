"""Small deterministic checks of stochastic decisions and information timing."""
from dataclasses import replace
from datetime import timedelta
from itertools import product
import math

import pandas as pd
import pytest

from solar_model.config import validate_config
from solar_model.planning import markov
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import OperatingMode
from tests.planning.test_dynamic_programming import period
from tests.planning.helpers import inputs


def device():
    return dp.DeviceParameters(1., 1., 1., 1., 4., 4., 4., 4.)


def choices():
    return [dp.Action(OperatingMode.CHARGE_PV_FIRST, 0.), dp.Action(OperatingMode.CHARGE_PV_FIRST, 1.), dp.Action(OperatingMode.DISCHARGE_PV_FIRST, 1.), dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)]


def test_exit_probability_scaling_and_joint_independence():
    supplied = [[.6, .3, .1], [.2, .6, .2], [.1, .3, .6]]
    for actual, expected in zip(markov.transition(3.), supplied):
        assert actual == pytest.approx(expected)
    for hours in (0., 1 / 12, .25, 1., 3., 6., 48.):
        matrix = markov.transition(hours)
        for i, row in enumerate(matrix):
            exit_probability = 1 - supplied[i][i]
            expected_exit = 1 - (1 - exit_probability) ** (hours / 3)
            assert row[i] == pytest.approx(1 - expected_exit)
            for j in range(3):
                if j != i:
                    assert row[j] == pytest.approx(expected_exit * supplied[i][j] / exit_probability)
            assert sum(row) == pytest.approx(1.) and min(row) >= 0
    joint = markov.joint_transition(3., tuple(tuple(row) for row in supplied), tuple(tuple(row) for row in supplied), 3.)
    assert joint[4][4] == pytest.approx(.36)
    assert all(sum(row) == pytest.approx(1.) and min(row) >= 0 for row in joint)
    for bad in (-1., math.nan, math.inf):
        with pytest.raises(ValueError):
            markov.transition(bad)


def test_yaml_transition_settings_round_trip_and_change_probabilities(tmp_path):
    from pathlib import Path
    import yaml
    from solar_model.config import energy_plan_yaml, load_config, parse_energy_plan_yaml
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    settings = raw["energy_plan"]["optimiser"]
    settings["transition_hours"] = 6.
    settings["transition_matrix"]["solar"] = [[.8, .15, .05], [.15, .7, .15], [.05, .15, .8]]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path)
    parsed = parse_energy_plan_yaml(config, energy_plan_yaml(config.energy_plan))
    actual = parsed.optimiser
    assert not hasattr(actual, "uncertainty_model")
    assert actual.transition_hours == 6.
    assert actual.transition_matrix.solar == tuple(tuple(row) for row in settings["transition_matrix"]["solar"])
    for row, expected in zip(markov.transition(6., actual.transition_matrix.solar, actual.transition_hours), actual.transition_matrix.solar):
        assert row == pytest.approx(expected)
    assert markov.joint_transition(6., actual.transition_matrix.solar, actual.transition_matrix.house_load, actual.transition_hours)[4][4] == pytest.approx(.42)
    assert markov.transition(3., actual.transition_matrix.solar, actual.transition_hours) != markov.transition(3.)


@pytest.mark.parametrize("matrix,hours", [
    (((1., 0.), (0., 1.)), 3.),
    (((.6, .3, .2), (.2, .6, .2), (.1, .3, .6)), 3.),
    (((-.1, .5, .6), (.2, .6, .2), (.1, .3, .6)), 3.),
    (((math.nan, .3, .1), (.2, .6, .2), (.1, .3, .6)), 3.),
    (((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)), 0.),
])
def test_invalid_configured_transitions_rejected(matrix, hours):
    from solar_model.planning.markov_transitions import validate_transition_inputs
    with pytest.raises(ValueError, match="optimiser.transition"):
        validate_transition_inputs(matrix, hours)


def test_identical_rows_and_certain_transitions_are_supported(inputs):
    matrix = ((.6, .3, .1), (.1, .1, .8), (.1, .1, .8))
    settings = replace(inputs[3].energy_plan.optimiser, transition_matrix=replace(inputs[3].energy_plan.optimiser.transition_matrix, solar=matrix, house_load=matrix))
    config = replace(inputs[3], energy_plan=replace(inputs[3].energy_plan, optimiser=settings))
    validate_config(config)
    for actual, expected in zip(markov.transition(3., matrix), matrix):
        assert actual == pytest.approx(expected)
    certain = ((0., 1., 0.), (1., 0., 0.), (0., 0., 1.))
    assert markov.transition(0., certain) == ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    assert markov.transition(.25, certain) == certain


def test_identity_matrix_keeps_conditions_fixed():
    identity = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    joint = markov.joint_transition(.25, identity, identity, 3.)
    assert joint == [[float(i == j) for j in range(9)] for i in range(9)]


def test_average_before_selecting_command_not_clairvoyant():
    periods = [period(hours=1., solar=0., load=1., import_rate=1., export_rate=0.), period(hours=1., solar=3., load=0., import_rate=1., export_rate=0.)]
    grid = [0., 1., 2.]
    next_values = [[0., .5, 1.], [0., .5, 1.]]
    transitions = [[.5, .5], [.5, .5]]
    commands = choices()[1:3]
    scores, selected = markov.best_choices(device(), 2., periods, grid, commands, transitions, next_values, 1.)
    outcome_scores = []
    for action in commands:
        outcome_scores.append([])
        for j, p in enumerate(periods):
            step = dp.simulate_system_single_time_period(device(), 2., p, 1., action)
            outcome_scores[-1].append(dp.step_score(step) + dp.interpolate(grid, next_values[j], step.battery_end))
    expected = max(sum(row) / 2 for row in outcome_scores)
    clairvoyant = sum(max(row[j] for row in outcome_scores) / 2 for j in range(2))
    assert scores == pytest.approx([expected, expected])
    assert expected < clairvoyant
    assert sum(outcome_scores[selected[0]]) / 2 == pytest.approx(expected)


def test_small_tree_exhaustive_equivalence_and_forward_score():
    grid = [0., 1., 2.]
    periods = [[period(hours=1., solar=0., load=0., import_rate=.1, export_rate=0.)] * 2, [period(hours=1., solar=0., load=1., import_rate=1., export_rate=0.), period(hours=1., solar=0., load=2., import_rate=1., export_rate=0.)]]
    transitions = [[[.8, .2], [.3, .7]]] * 2
    actions = choices()
    values, policy = markov.backward_values(device(), 2., periods, grid, actions, transitions, 0.)
    # Enumerate the initial command and a later command for each observed condition.
    best = -math.inf
    for a0, a_low, a_high in product(range(len(actions)), repeat=3):
        score = 0.
        for j in range(2):
            first = dp.simulate_system_single_time_period(device(), 2., periods[0][j], 0., actions[a0])
            for k in range(2):
                second = dp.simulate_system_single_time_period(device(), 2., periods[1][k], first.battery_end, actions[(a_low, a_high)[j]])
                score += transitions[0][0][j] * transitions[1][j][k] * (first.score + second.score)
        best = max(best, score)
    assert values[0][0][0] == pytest.approx(best)
    assert actions[policy[0][0][0]].mode == OperatingMode.CHARGE_PV_FIRST
    assert actions[policy[0][0][0]].rate_kw == 1.
    totals = markov.forward_distribution(device(), 2., periods, grid, actions, transitions, policy, 0, 0)
    expected = markov.expectation_frame([row[0] for row in periods], totals, 0.)
    assert expected.total_score.sum() == pytest.approx(best)
    assert expected.grid_import_kwh.iloc[0] == pytest.approx(1.)


def test_supported_infeasibility_is_not_renormalised():
    limited = device()._replace(grid_max_import_power_kw=1.)
    periods = [period(hours=1., solar=0., load=0.), period(hours=1., solar=0., load=20.)]
    grid = [0., 1., 2.]
    next_values = [[0.] * 3] * 2
    scores, selected = markov.best_choices(limited, 2., periods, grid, choices(), [[.999, .001], [1., 0.]], next_values, 0.)
    assert scores[0] == -math.inf and selected[0] == -1
    assert math.isfinite(scores[1]) and selected[1] >= 0


def test_no_charge_when_future_import_is_cheaper():
    periods = [[period(hours=1., solar=0., load=0., import_rate=1., export_rate=0.)], [period(hours=1., solar=0., load=1., import_rate=.1, export_rate=0.)]]
    values, policy = markov.backward_values(device(), 2., periods, [0., 1., 2.], choices(), [[[1.]], [[1.]]], 0.)
    assert choices()[policy[0][0][0]].rate_kw == 0.
    assert values[0][0][0] == pytest.approx(-.1)


def test_python_numba_parity_with_interpolation():
    from numba.typed import List
    periods = [[period(hours=.25, solar=0., load=.3), period(hours=.25, solar=.4, load=.2)]] * 2
    transitions = [[[.8, .2], [.3, .7]]] * 2
    grid = [0., 1., 2.]
    expected_values, expected_policy = markov.backward_values(device(), 2., periods, grid, choices(), transitions, .2)
    compiled_backward, compiled_forward = markov.compiled_functions()
    ps = List([List(row) for row in periods])
    ts = List([List([List(row) for row in matrix]) for matrix in transitions])
    gs, cs = List(grid), List(choices())
    values, policy = compiled_backward(device(), 2., ps, gs, cs, ts, .2)
    for actual_t, expected_t in zip(values, expected_values):
        for actual, expected in zip(actual_t, expected_t):
            assert actual == pytest.approx(expected)
    assert policy == expected_policy
    typed_policy = List([List([List(row) for row in time]) for time in policy])
    expected = markov.forward_distribution(device(), 2., periods, grid, choices(), transitions, policy, 1, 0)
    actual = compiled_forward(device(), 2., ps, gs, cs, ts, typed_policy, 1, 0)
    for a, e in zip(actual, expected):
        assert a == pytest.approx(e)
    frame = markov.expectation_frame([row[0] for row in periods], actual, .2)
    assert frame.total_score.sum() == pytest.approx(values[0][0][1])


@pytest.mark.parametrize("compile_model_to_native_code", [False, True])
def test_public_markov_result_and_fixed_stress_reports(inputs, compile_model_to_native_code, tmp_path):
    hardware, state, single, config, start = inputs
    settings = replace(config.energy_plan.optimiser, compile_model_to_native_code=compile_model_to_native_code, projection_resolution_mins=(15,) * 4)
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=1, num_visible_projection_hours=1, optimiser=settings))
    scenarios = create_scenarios(single[0].forecast_data, config.energy_plan.scenario_multiplier)
    result = dp.simulate_energy_plan(hardware, state, scenarios, config, start, grid_step_percent=25., power_divisions=1)
    assert set(result.results) == set(markov.NAMES)
    assert result.expected_score == pytest.approx(result.expected_results.total_score.sum())
    assert result.expected_lookahead_results.empty
    assert result.expected_results.starts_at.iloc[0] == start
    assert result.expected_results.ends_at.iloc[-1] == start + timedelta(hours=1)
    assert result.expected_results.battery_start_kwh.iloc[0] == pytest.approx(state.battery_kwh)
    # Exercise the real model report through publication, independently of UI data.
    from solar_model.services.plan_history import persist_report, read_saved_run
    from solar_model.services.planning import PlanReport
    from solar_model.storage.migrations import open_database
    path = str(tmp_path / "saved-plan.db")
    database = open_database(path)
    saved_config = replace(config, database=replace(config.database, path=path))
    try:
        report = PlanReport(result, pd.DataFrame(), hardware.system_battery_capacity_kwh, config.energy_plan.scenario_multiplier, 3., 50., hardware.system_battery_capacity_kwh, start)
        snapshot_id = persist_report(saved_config, report, start, start)
        saved = read_saved_run(saved_config, snapshot_id)
        assert saved.report.simulation.expected_score == result.expected_score
        pd.testing.assert_frame_equal(saved.report.simulation.expected_results, result.expected_results)
        for name in result.results:
            pd.testing.assert_frame_equal(saved.report.simulation.results[name], result.results[name])
    finally:
        database.close()
    # High-case timing redistribution has been removed, retaining multiplier-only amounts.
    high = result.results["high_high"]
    assert high.load_kwh.iloc[0] == pytest.approx(.25 * config.energy_plan.scenario_multiplier.house_load.high)
    if not compile_model_to_native_code:
        from zipfile import ZipFile
        from solar_model.tools.reports import group_plan, write_workbook
        grouped = {name: group_plan(frame, 60) for name, frame in result.results.items()}
        assert grouped["med"].total_score.sum() == pytest.approx(result.results["med"].total_score.sum())
        output = tmp_path / "markov.xlsx"
        write_workbook(grouped, output, start, expected=result.expected_results, expected_score=result.expected_score)
        with ZipFile(output) as workbook:
            assert 'name="Expected visible"' in workbook.read("xl/workbook.xml").decode()
            assert "Fixed-condition stress case" in workbook.read("xl/sharedStrings.xml").decode()


def test_persistence_changes_the_distribution_of_future_demand():
    periods = [[period(hours=1., solar=0., load=0., import_rate=1., export_rate=0.), period(hours=1., solar=0., load=2., import_rate=1., export_rate=0.)]] * 2
    hold = [choices()[0]]
    persistent, _ = markov.backward_values(device(), 2., periods, [0., 1., 2.], hold, [[[.9, .1], [.1, .9]]] * 2, 0.)
    independent, _ = markov.backward_values(device(), 2., periods, [0., 1., 2.], hold, [[[.5, .5], [.5, .5]]] * 2, 0.)
    assert persistent[0][0][0] == pytest.approx(-.56)
    assert independent[0][0][0] == pytest.approx(-2.)


def test_lookahead_partial_intervals_preserve_expectation_with_reordered_inputs(inputs):
    hardware, state, single, config, original_start = inputs
    start = original_start + timedelta(minutes=5)
    settings = replace(config.energy_plan.optimiser, compile_model_to_native_code=False, projection_resolution_mins=(15,) * 4 + (60,))
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=2, num_visible_projection_hours=1, optimiser=settings))
    scenarios = create_scenarios(single[0].forecast_data, config.energy_plan.scenario_multiplier)
    first = dp.simulate_energy_plan(hardware, state, scenarios, config, start, grid_step_percent=25., power_divisions=1)
    changed = list(reversed(scenarios))
    second = dp.simulate_energy_plan(hardware, state, changed, config, start, grid_step_percent=25., power_divisions=1)
    assert first.expected_score == pytest.approx(second.expected_score)
    assert first.next_action == second.next_action
    assert first.expected_results.ends_at.iloc[-1] == start + timedelta(hours=1)
    assert first.expected_lookahead_results.starts_at.iloc[0] == start + timedelta(hours=1)
    assert first.expected_lookahead_results.ends_at.iloc[-1] == start + timedelta(hours=2)
    assert first.expected_results.total_score.sum() + first.expected_lookahead_results.total_score.sum() == pytest.approx(first.expected_score)
    assert first.expected_results.terminal_score.sum() == 0.


def test_solar_and_house_load_matrices_control_their_own_joint_dimensions():
    identity = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    cycle = ((0., 1., 0.), (0., 0., 1.), (1., 0., 0.))
    load_only = markov.joint_transition(3., identity, cycle, 3.)
    solar_only = markov.joint_transition(3., cycle, identity, 3.)
    assert load_only[4][7] == 1. and sum(load_only[4]) == 1.
    assert solar_only[4][5] == 1. and sum(solar_only[4]) == 1.
    assert all(sum(row) == pytest.approx(1.) for row in load_only + solar_only)
