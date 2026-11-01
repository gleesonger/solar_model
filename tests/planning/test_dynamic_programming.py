"""Targeted physical, search and shared-command checks for the DP experiment."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import product
import subprocess
import sys

import pytest
import pandas as pd
import numpy as np

from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import ForecastData, OperatingMode, SimulationResult
from tests.planning.test_next_action import inputs, simulate


def period(**changes):
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    base = dp.Period(start.timestamp(), (start + timedelta(minutes=30)).timestamp(), 0., .5, .4, .1, "flat", 0., 10., 9999., .5, 0., 4.)
    return base._replace(**{name: value if name == "tariff_name" else float(value) for name, value in changes.items()})


def test_epoch_periods_preserve_fractional_utc_report_timestamps(inputs):
    start = inputs[-1].replace(microsecond=123456)
    end = start + timedelta(minutes=30)
    p = period(starts_at=start.timestamp(), ends_at=end.timestamp())
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, 2., dp.Action(OperatingMode.SELF_CONSUMPTION, 0.))
    frame = dp.result_frame(inputs[0], [p], [step], 2., .2)
    assert frame.starts_at.iloc[0] == start
    assert frame.ends_at.iloc[0] == end
    assert str(frame.starts_at.dt.tz) == "UTC"
    assert frame.tariff_name.tolist() == ["flat"]


def test_numba_import_and_compilation_are_lazy():
    code = """
import builtins
import sys
from solar_model.planning import plan_optimisation_dynamic_programming as dp

original_import = builtins.__import__
def block_numba(name, *args, **kwargs):
    if name == "numba" or name.startswith("numba."):
        raise AssertionError("Python execution imported Numba")
    return original_import(name, *args, **kwargs)

builtins.__import__ = block_numba
device = dp.DeviceParameters(.95, .95, 5., 5., 5., 5.)
periods = [dp.Period(0., 900., 0., .25, .4, .1, "flat", 0., 10., 9999., .25, .01, 3.)]
grids = [[0., 5., 10.], [0., 5., 10.]]
choices = [dp.Action(dp.OperatingMode.SELF_CONSUMPTION, 0.)]
values = dp.backward_values(device, 10., periods, grids, choices, .2)
expected = dp.forward_projection(device, 10., periods, grids, values, choices, 5., choices[0])
assert expected is not None
assert "numba" not in sys.modules
builtins.__import__ = original_import
backward, forward = dp.get_numba_functions()
assert not backward.signatures
assert not forward.signatures
from numba.typed import List
periods = List(periods)
grids = List([List(grid) for grid in grids])
choices = List(choices)
actual_values = backward(device, 10., periods, grids, choices, .2)
assert actual_values == values
actual_values = List([List(row) for row in actual_values])
actual = forward(device, 10., periods, grids, actual_values, choices, 5., choices[0])
assert actual == expected
assert dp.get_numba_functions() == (backward, forward)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_numba_and_python_match_value_tables_and_bounded_paths(inputs):
    from numba.typed import List

    device = replace(inputs[0], charge_efficiency=.95, discharge_efficiency=.9)
    periods = [period(solar=.8, load=.2, protection_weight=.02), period(solar=.1, load=.8, export_limit=0., protection_weight=.02)]
    grids = dp.bounded_grids([float(i) for i in range(11)], 10., [(.1, .4), (0., .5)])
    choices = dp.actions(device, inputs[3].energy_plan)
    numerical_device = dp.DeviceParameters(*(getattr(device, field) for field in dp.DeviceParameters._fields))
    python_values = dp.backward_values(numerical_device, 10., periods, grids, choices, .2)
    numerical_periods = List(periods)
    numerical_grids, numerical_choices = List([List(grid) for grid in grids]), List(choices)
    backward, forward = dp.get_numba_functions()
    numba_values = backward(numerical_device, 10., numerical_periods, numerical_grids, numerical_choices, .2)
    for expected, actual in zip(python_values, numba_values):
        np.testing.assert_allclose(actual, expected, atol=1e-12, rtol=1e-12)
    numba_values = List([List(row) for row in numba_values])
    for action in choices:
        expected = dp.forward_projection(numerical_device, 10., periods, grids, python_values, choices, 2., action)
        actual = forward(numerical_device, 10., numerical_periods, numerical_grids, numba_values, numerical_choices, 2., action)
        assert (actual is None) == (expected is None)
        if actual is not None:
            for expected_step, actual_step in zip(expected, actual):
                assert actual_step == pytest.approx(expected_step, abs=1e-12)
    signatures = (tuple(backward.signatures), tuple(forward.signatures))
    changed_periods = List([numerical_periods[0]._replace(solar=.4), *numerical_periods])
    changed_grids = List([numerical_grids[0], *numerical_grids])
    changed_values = backward(numerical_device, 10., changed_periods, changed_grids, numerical_choices, .2)
    forward(numerical_device, 10., changed_periods, changed_grids, List([List(row) for row in changed_values]), numerical_choices, 2., choices[0])
    assert (tuple(backward.signatures), tuple(forward.signatures)) == signatures


def test_numba_and_python_match_complete_configured_pipeline(inputs):
    device, state, scenarios, config, start = inputs
    tariff = config.tariffs.periods[0]
    rule = tariff.import_[0]
    tariff = replace(tariff, import_=(replace(rule, end_time="00:30", name="early"), replace(rule, start_time="00:30", name="later")))
    config = replace(config, timezone="UTC", tariffs=replace(config.tariffs, periods=(tariff,)))
    scenarios = create_scenarios(scenarios[0].forecast_data, config.energy_plan.scenarios)
    results = []
    for use_numba in (False, True):
        settings = replace(config.energy_plan.optimiser.dynamic_programming, use_numba=use_numba)
        configured = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, dynamic_programming=settings)))
        results.append(dp.simulate_energy_plan(device, state, scenarios, configured, start))
    expected, actual = results
    assert len(actual.results) == 9
    assert actual.next_action == expected.next_action
    for name in expected.results:
        assert actual.results[name].starts_at.iloc[0] == start
        assert actual.results[name].ends_at.iloc[-1] == start + timedelta(hours=1)
        assert actual.lookahead_results[name].starts_at.iloc[0] == start + timedelta(hours=1)
        assert actual.lookahead_results[name].ends_at.iloc[-1] == start + timedelta(hours=25)
        assert actual.results[name].tariff_name.tolist() == ["early", "early", "later", "later"]
        pd.testing.assert_frame_equal(actual.results[name], expected.results[name], atol=1e-10, rtol=1e-12)
        pd.testing.assert_frame_equal(actual.lookahead_results[name], expected.lookahead_results[name], atol=1e-10, rtol=1e-12)


def test_default_refinement_retains_better_paths_and_centres_next_bounds_on_them(inputs, monkeypatch, caplog):
    caplog.set_level("INFO", logger=dp.LOGGER.name)
    action = dp.NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0., 100., 0.)
    coarse = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [5.], "total_score": [1.]})}, {"base": pd.DataFrame({"battery_end_kwh": [5.], "total_score": [1.]})})
    improved = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [6.], "total_score": [2.]})}, {"base": pd.DataFrame({"battery_end_kwh": [6.], "total_score": [2.]})})
    worse = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [4.], "total_score": [.5]})}, {"base": pd.DataFrame({"battery_end_kwh": [4.], "total_score": [.5]})})
    calls = []

    def fake_pass(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 4:
            raise dp.NoFeasiblePlanError("unable to find a feasible energy plan on the dynamic-programming grid")
        return [coarse, improved, worse][len(calls) - 1]

    monkeypatch.setattr(dp, "solve_pass", fake_pass)
    assert dp.simulate_energy_plan(*inputs) is improved
    summaries = [record for record in caplog.records if record.getMessage().startswith("DP ") and record.levelname == "INFO"]
    assert len(summaries) == 6
    assert all(record.levelname == "INFO" for record in summaries)
    assert "DP Model Start - num_projection_hours=25 num_time_steps=10 refinement_stages=4 use_numba=True" in caplog.text
    assert "DP Refinement Stage #4 infeasible - retaining previous result" in caplog.text
    assert "accepted=False" in summaries[-2].getMessage()
    completed = [record for record in summaries if record.getMessage().startswith("DP Project end")]
    assert len(completed) == 1 and completed[0] is summaries[-1]
    assert "elapsed_seconds=" in completed[0].getMessage()
    stages = inputs[3].energy_plan.optimiser.dynamic_programming.refinement_stages
    assert [(call["grid_step_percent"], call["power_divisions"]) for call in calls] == [(stage.grid_step_percent, stage.power_divisions) for stage in stages]
    for call, centre, width in zip(calls[1:], [.5, .6, .6], [.05, .03, .01]):
        for pair in call["battery_bounds"]["base"]:
            assert pair == pytest.approx((centre - width, centre + width))


def test_stage_logging_separates_nested_compilation_from_projection(inputs, monkeypatch, caplog):
    from numba.core import event

    caplog.set_level("INFO", logger=dp.LOGGER.name)
    clock = [0.]
    monkeypatch.setattr(dp, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(event, "timer", lambda: clock[0])
    action = dp.NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0., 100., 0.)
    result = SimulationResult(action, {"base": pd.DataFrame({"total_score": [2.]})}, {"base": pd.DataFrame({"total_score": [1.]})})

    def fake_pass(*args, **kwargs):
        clock[0] += 1.
        with event.trigger_event("numba:compile"):
            clock[0] += 2.
            with event.trigger_event("numba:compile"):
                clock[0] += 3.
        clock[0] += 1.
        return result

    monkeypatch.setattr(dp, "solve_pass", fake_pass)
    assert dp.simulate_energy_plan(*inputs, grid_step_percent=2.) is result
    stage = next(record for record in caplog.records if record.getMessage().startswith("DP Refinement Stage"))
    assert stage.args == (1, 5., 2., 3., True)
    final = caplog.records[-1]
    assert final.args == (7., 5., 2., 3.)
    assert "refinement_stages=1" in caplog.text

    caplog.clear()
    monkeypatch.setattr(dp, "solve_pass", lambda *args, **kwargs: result)
    dp.simulate_energy_plan(*inputs, grid_step_percent=2.)
    assert "compilation_seconds=0.000 projection_seconds=" in caplog.text


def test_failed_project_logs_failure_without_success(inputs, monkeypatch, caplog):
    caplog.set_level("INFO", logger=dp.LOGGER.name)

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(dp, "solve_pass", fail)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        dp.simulate_energy_plan(*inputs)
    assert "DP Project failed - elapsed_seconds=" in caplog.text
    assert "DP Project end" not in caplog.text


def test_infeasible_project_logs_message_without_traceback(inputs, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise dp.NoFeasiblePlanError("unable to find a feasible energy plan on the dynamic-programming grid")

    monkeypatch.setattr(dp, "solve_pass", fail)
    with pytest.raises(dp.NoFeasiblePlanError):
        dp.simulate_energy_plan(*inputs)
    assert "DP unable to calculate a plan" in caplog.text
    assert "DP Project end" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dp_aligns_first_hour_and_preserves_elapsed_horizon_through_dst(inputs):
    config = replace(inputs[3], energy_plan=replace(inputs[3].energy_plan, num_projection_hours=24, num_visible_projection_hours=24, optimiser=replace(inputs[3].energy_plan.optimiser, dynamic_programming=replace(inputs[3].energy_plan.optimiser.dynamic_programming, projection_resolution_mins=(15,) * 96))))
    start = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    intervals = dp.projection_intervals(config, start)
    assert len(intervals) == 97
    assert intervals[0][0] == start
    assert intervals[-1][1] == start + timedelta(hours=24)
    assert [(end - begin).total_seconds() / 60 for begin, end in intervals] == [15] * 3 + [10] + [15] * 92 + [5]


def test_real_default_pipeline_returns_best_completed_stage(inputs, monkeypatch):
    original = dp.solve_pass
    completed = []

    def record_pass(*args, **kwargs):
        result = original(*args, **kwargs)
        completed.append(result)
        return result

    monkeypatch.setattr(dp, "solve_pass", record_pass)
    result = dp.simulate_energy_plan(*inputs)
    assert len(completed) >= 1
    assert len(result.results["base"]) == 4
    assert dp.result_score(result, inputs[2]) == pytest.approx(max(dp.result_score(candidate, inputs[2]) for candidate in completed))


def test_dp_calculations_do_not_call_milp_or_shared_physics_helpers(inputs, monkeypatch):
    from solar_model.planning import plan_optimisation_milp as optimisation, system

    def unexpected(*args, **kwargs):
        raise AssertionError("DP called a MILP/shared calculation")

    for name in ("_projection_intervals", "_protection_value", "_reference_import_rate", "_terminal_rate", "battery_protection_score"):
        monkeypatch.setattr(optimisation, name, unexpected)
    monkeypatch.setattr(system, "system_limits", unexpected)
    monkeypatch.setattr(system, "allocate_energy_routes", unexpected)
    result = dp.solve_pass(*inputs)
    assert len(result.results["base"]) == 4


def test_actions_span_minimum_to_maximum_and_interpolation_keeps_physical_energy_separate(inputs):
    choices = dp.actions(inputs[0], inputs[3].energy_plan)
    assert len(choices) == 11
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx([.88, 1.66, 2.44, 3.22, 4.])
    assert dp.interpolate([0., 1.], [0., 10.], .37) == pytest.approx(3.7)
    assert dp.interpolate([0., 1.], [-float("inf"), 10.], .37) == -float("inf")


def test_tenths_option_generates_twenty_one_commands(inputs):
    choices = dp.actions(inputs[0], inputs[3].energy_plan, power_divisions=10)
    assert len(choices) == 21
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        rates = [action.rate_kw for action in choices if action.mode == mode]
        assert rates == pytest.approx([.49, .88, 1.27, 1.66, 2.05, 2.44, 2.83, 3.22, 3.61, 4.])


@pytest.mark.parametrize("energy,expected", [(0., 0.), (2., 100 * (1 - .3 ** .5)), (4., 70.), (8., 80.)])
def test_transition_uses_exact_protection_curve(inputs, energy, expected):
    p = period(solar=0., load=0., protection_weight=.2, preferred_reserve_kwh=4.)
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, energy, dp.Action(OperatingMode.SELF_CONSUMPTION, 0.))
    assert step.protection_score == pytest.approx(.2 * expected, abs=1e-12)
    assert step.score == pytest.approx(step.protection_score - step.net_cost)


@pytest.mark.parametrize("maximum,expected", [(.05, []), (.1, [.1]), (.15, [.11, .12, .13, .14, .15])])
def test_minimum_power_choices_are_unique_and_within_hardware_limits(inputs, maximum, expected):
    device = replace(inputs[0], inverter_battery_charge_power_kw=maximum, inverter_battery_discharge_power_kw=maximum)
    choices = dp.actions(device, inputs[3].energy_plan)
    assert choices[0] == dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx(expected)


def test_charge_and_discharge_use_separate_minimum_power(inputs):
    settings = replace(inputs[3].energy_plan, min_forced_charge_power_kw=1., min_forced_discharge_power_kw=2.)
    choices = dp.actions(inputs[0], settings)
    assert [action.rate_kw for action in choices if action.mode == OperatingMode.CHARGE_PV_FIRST] == pytest.approx([1.6, 2.2, 2.8, 3.4, 4.])
    assert [action.rate_kw for action in choices if action.mode == OperatingMode.DISCHARGE_PV_FIRST] == pytest.approx([2.4, 2.8, 3.2, 3.6, 4.])


@pytest.mark.parametrize("maximum,expected", [(1., [.28, .46, .64, .82, 1.]), (999., [.88, 1.66, 2.44, 3.22, 4.]), (.05, [])])
def test_forced_power_maximum_caps_choices_or_disables_mode(inputs, maximum, expected):
    settings = replace(inputs[3].energy_plan, max_forced_charge_power_kw=maximum, max_forced_discharge_power_kw=maximum)
    choices = dp.actions(inputs[0], settings)
    assert choices[0] == dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx(expected)


@pytest.mark.parametrize("mode,minimum,maximum,expected", [
    (OperatingMode.CHARGE_PV_FIRST, 0., 2.5, 2.5),
    (OperatingMode.DISCHARGE_PV_FIRST, 1.5, 10., 1.5),
])
def test_energy_stopping_is_encoded_in_the_returned_command(inputs, mode, minimum, maximum, expected):
    p = period(minimum=minimum, maximum=maximum)
    action = (mode, 4.)
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, 2., action)
    command = dp.command(action, p, 10.)
    assert step.battery_end == pytest.approx(expected)
    assert command.charge_cut_off_soc_percent == pytest.approx(maximum * 10)
    assert command.discharge_cut_off_soc_percent == pytest.approx(minimum * 10)


def test_user_bounds_and_export_limits_reject_unsupported_behavior(inputs):
    device = inputs[0]
    assert dp.simulate_system_single_time_period(device, 10., period(minimum=1.8), 2., (OperatingMode.SELF_CONSUMPTION, 0.)) is None
    assert dp.simulate_system_single_time_period(device, 10., period(minimum=1.5, export_limit=0.), 2., (OperatingMode.DISCHARGE_PV_FIRST, 4.)) is None
    step = dp.simulate_system_single_time_period(device, 10., period(export_limit=0.), 2., (OperatingMode.DISCHARGE_PV_FIRST, 1.))
    assert step.discharge == pytest.approx(.5)
    assert step.grid_export == pytest.approx(0.)


def test_backward_values_match_exhaustive_search_when_states_align(inputs):
    device = replace(inputs[0], inverter_battery_charge_power_kw=2., inverter_battery_discharge_power_kw=2.)
    periods = [period(load=0., import_rate=.1, export_rate=.1, maximum=2.), period(load=0., import_rate=.5, export_rate=.5, maximum=2.)]
    choices = [(OperatingMode.SELF_CONSUMPTION, 0.), (OperatingMode.CHARGE_PV_FIRST, 2.), (OperatingMode.DISCHARGE_PV_FIRST, 2.)]
    best = -float("inf")
    for schedule in product(choices, repeat=2):
        battery, score = 0., 0.
        for p, action in zip(periods, schedule):
            step = dp.simulate_system_single_time_period(device, 2., p, battery, action)
            if step is None:
                break
            score += step.score
            battery = step.battery_end
        else:
            best = max(best, score)
    values = dp.backward_values(device, 2., periods, [[0., 1., 2.]] * 3, choices, 0.)
    assert best == pytest.approx(.4)
    assert values[0][0] == pytest.approx(best)


def test_period_bounds_restrict_paths_without_changing_soc_stops(inputs):
    p = period(load=0.)
    grids = dp.bounded_grids([0., 2., 4., 6., 8., 10.], 10., [(.2, .3)])
    assert grids[1] == [2., 3.]
    charge = dp.Action(OperatingMode.CHARGE_PV_FIRST, 2.)
    choices = [dp.Action(OperatingMode.SELF_CONSUMPTION, 0.), charge]
    values = dp.backward_values(inputs[0], 10., [p], grids, choices, 1.)
    path = dp.forward_projection(inputs[0], 10., [p], grids, values, choices, 2., charge)
    assert path[0].battery_end == pytest.approx(3.)
    assert dp.command(charge, p, 10.).charge_cut_off_soc_percent == 100.
    outside = dp.Action(OperatingMode.CHARGE_PV_FIRST, 4.)
    assert dp.simulate_system_single_time_period(inputs[0], 10., p, 2., outside).battery_end == pytest.approx(4.)
    assert dp.forward_projection(inputs[0], 10., [p], grids, values, choices, 2., outside) is None


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("reordered", [False, True])
@pytest.mark.parametrize("changes", [{}, {"minimum": 4., "maximum": 6.}, {"solar": 2., "load": .3, "export_limit": 0.}])
def test_pruning_preserves_exhaustive_values_and_paths_and_skips_simulations(bounded, reordered, changes, monkeypatch):
    device = dp.DeviceParameters(.95, .9, 2., 2., 2., 2.)
    periods = [period(**{"load": 0., **changes})] * 2
    nodes = [float(i) for i in range(11)]
    grids = [nodes, [4., 5., 6.], [4., 5., 6.]] if bounded else [nodes] * 3
    first = dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)
    choices = [first] + [dp.Action(mode, float(rate)) for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST) for rate in range(1, 11)]
    if reordered:
        choices = choices[::2] + choices[1::2][::-1]
    simulate_period = dp.simulate_system_single_time_period

    # Exhaustive reference: evaluate every command, including capped and out-of-corridor ones.
    expected = [[.2 * battery for battery in grids[-1]]]
    for t in (1, 0):
        row = []
        for battery in grids[t]:
            steps = [simulate_period(device, 10., periods[t], battery, action) for action in choices]
            row.append(max((step.score + dp.interpolate(grids[t + 1], expected[0], step.battery_end) for step in steps if step is not None), default=-float("inf")))
        expected.insert(0, row)
    expected_path = []
    battery = 5.
    for t, p in enumerate(periods):
        candidates = [simulate_period(device, 10., p, battery, action) for action in ([first] if t == 0 else choices)]
        feasible = [(step.score + dp.interpolate(grids[t + 1], expected[t + 1], step.battery_end), step) for step in candidates if step is not None]
        best_score, best_step = max(feasible, key=lambda candidate: candidate[0], default=(-float("inf"), None))
        if best_score == -float("inf"):
            expected_path = None
            break
        expected_path.append(best_step)
        battery = best_step.battery_end

    calls = []

    def counted_simulation(*args):
        calls.append(args[-1])
        return simulate_period(*args)

    monkeypatch.setattr(dp, "simulate_system_single_time_period", counted_simulation)
    values = dp.backward_values(device, 10., periods, grids, choices, .2)
    assert values == expected
    assert len(calls) < sum(len(grid) for grid in grids[:-1]) * len(choices)
    calls.clear()
    assert dp.forward_projection(device, 10., periods, grids, values, choices, 5., first) == expected_path
    assert len(calls) < 1 + len(choices)


@pytest.mark.parametrize("bounds", [[(-.1, 1.)], [(.8, .2)], [(0., float("nan"))], [(0., 1.1)]])
def test_invalid_search_bounds_are_rejected(bounds):
    with pytest.raises(ValueError, match="battery bounds"):
        dp.bounded_grids([0., 10.], 10., bounds)


def test_full_visible_bounds_match_default_and_pad_hidden_periods(inputs):
    expected = dp.solve_pass(*inputs)
    bounds = [(0., 1.)] * len(next(iter(expected.results.values())))
    actual = dp.solve_pass(*inputs, battery_bounds=bounds)
    assert actual.next_action == expected.next_action
    for name in expected.results:
        assert actual.results[name].equals(expected.results[name])
        assert actual.lookahead_results[name].equals(expected.lookahead_results[name])


def test_nine_scenario_result_replays_the_same_first_command_as_lp(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=4.), *base.solar[1:]], base.load)
    scenarios = create_scenarios(forecast, config.energy_plan.scenarios)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, dynamic_programming=replace(config.energy_plan.optimiser.dynamic_programming, projection_resolution_mins=(30, 30) + (240,) * 6))))
    args = (device, state, scenarios, config, start)
    result = dp.solve_pass(*args)
    replay = simulate(args, result.next_action, return_result=True)
    assert len(result.results) == 9
    for name, frame in result.results.items():
        actual, expected = frame.iloc[0], replay.results[name].iloc[0]
        for column in ("battery_end_kwh", "grid_import_kwh", "grid_export_kwh", "solar_clipped_kwh"):
            assert actual[column] == pytest.approx(expected[column], abs=1e-6)
        assert len(frame) == 2
        assert len(result.lookahead_results[name]) == 6
        assert result.lookahead_results[name].battery_start_kwh.iloc[0] == pytest.approx(frame.battery_end_kwh.iloc[-1])


def test_forced_first_command_is_shared_and_selected_using_all_nine_scenarios(inputs, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=dp.LOGGER.name)
    device, state, scenarios, config, start = inputs
    settings = replace(config.energy_plan.optimiser.dynamic_programming, use_numba=False)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, dynamic_programming=settings)))
    device = replace(device, inverter_max_absorption_power_kw=1.)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=1.), *base.solar[1:]], base.load)
    scenarios = create_scenarios(forecast, config.energy_plan.scenarios)
    tariff = config.tariffs.periods[0]
    rule = tariff.import_[0]
    tariff = replace(tariff, import_=(replace(rule, end_time="00:30", rate=.01), replace(rule, start_time="00:30", rate=.4)))
    config = replace(config, timezone="UTC", tariffs=replace(config.tariffs, periods=(tariff,)))
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, dynamic_programming=replace(config.energy_plan.optimiser.dynamic_programming, projection_resolution_mins=(30, 30) + (240,) * 6))))
    original = dp.forward_projection
    records = []

    def record_forward_projection(*args):
        assert isinstance(args[0], dp.DeviceParameters)
        path = original(*args)
        records.append((args[-1], args[2], path))
        return path

    monkeypatch.setattr(dp, "forward_projection", record_forward_projection)
    result = dp.solve_pass(device, state, scenarios, config, start)
    assert not any(record.getMessage().startswith("DP Project end") for record in caplog.records)
    timings = [record for record in caplog.records if record.getMessage().startswith("dp_model timings:")]
    assert len(timings) == 1 and timings[0].levelname == "DEBUG"
    assert "backward_seconds=" in timings[0].getMessage()
    assert "forward_seconds=" in timings[0].getMessage()
    assert result.next_action.mode == OperatingMode.CHARGE_PV_FIRST
    chosen = (result.next_action.mode, result.next_action.force_battery_charge_rate)
    selected = [record for record in records if record[0] == chosen]
    assert len(selected) == 9
    assert len({id(periods) for action, periods, path in selected}) == 9
    assert len({(periods[0].solar, periods[0].load) for action, periods, path in selected}) == 9
    assert len({round(path[0].battery_end, 6) for action, periods, path in selected}) > 1

    terminal_rate = dp.terminal_rate(config, next(iter(result.lookahead_results.values())).ends_at.iloc[-1]) * device.discharge_efficiency
    scores = {}
    for action in dp.actions(device, config.energy_plan):
        candidate = [record for record in records if record[0] == action]
        if len(candidate) == 9 and all(path is not None for action, periods, path in candidate):
            scores[action] = sum(scenario.probability * (sum(step.score for step in record[2]) + terminal_rate * record[2][-1].battery_end) for scenario, record in zip(scenarios, candidate))
    assert scores[chosen] == pytest.approx(max(scores.values()))
    replay = simulate((device, state, scenarios, config, start), result.next_action, return_result=True)
    for scenario, (action, periods, path) in zip(scenarios, selected):
        assert dp.command(action, periods[0], 10.) == result.next_action
        row = result.results[scenario.name].iloc[0]
        assert row.battery_end_kwh == pytest.approx(path[0].battery_end)
        assert row.grid_import_kwh == pytest.approx(path[0].grid_import)
        assert row.grid_export_kwh == pytest.approx(path[0].grid_export)
        expected = replay.results[scenario.name].iloc[0]
        for column in ("battery_end_kwh", "grid_import_kwh", "grid_export_kwh", "solar_clipped_kwh"):
            assert row[column] == pytest.approx(expected[column], abs=1e-6)
