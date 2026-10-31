"""Targeted physical, search and shared-command checks for the DP experiment."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest
import pandas as pd

from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import ForecastData, OperatingMode, SimulationResult
from tests.planning.test_next_action import inputs, simulate


def period(**changes):
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    base = dp.Period(start, start + timedelta(minutes=30), 0., .5, .4, .1, "flat", 0., 10., 9999., .5, 0., 4.)
    return replace(base, **changes)


def test_default_refinement_retains_better_paths_and_centres_next_bounds_on_them(inputs, monkeypatch):
    action = dp.NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0., 100., 0.)
    coarse = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [5.], "total_score": [1.]})}, {"base": pd.DataFrame({"battery_end_kwh": [5.], "total_score": [1.]})})
    improved = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [6.], "total_score": [2.]})}, {"base": pd.DataFrame({"battery_end_kwh": [6.], "total_score": [2.]})})
    worse = SimulationResult(action, {"base": pd.DataFrame({"battery_end_kwh": [4.], "total_score": [.5]})}, {"base": pd.DataFrame({"battery_end_kwh": [4.], "total_score": [.5]})})
    calls = []

    def fake_pass(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 4:
            raise ValueError("unable to find a feasible energy plan on the dynamic-programming grid")
        return [coarse, improved, worse][len(calls) - 1]

    monkeypatch.setattr(dp, "solve_pass", fake_pass)
    assert dp.simulate_energy_plan(*inputs) is improved
    assert [(call["grid_step_percent"], call["power_divisions"]) for call in calls] == [(2., 5), (1., 10), (1., 20), (1., 40)]
    for call, centre, width in zip(calls[1:], [.5, .6, .6], [.05, .03, .01]):
        for pair in call["battery_bounds"]["base"]:
            assert pair == pytest.approx((centre - width, centre + width))


def test_default_dp_grid_is_quarter_hour_in_utc_through_dst(inputs):
    config = replace(inputs[3], energy_plan=replace(inputs[3].energy_plan, num_projection_hours=24))
    start = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    intervals = dp.projection_intervals(config, start)
    assert len(intervals) == 96
    assert intervals[0][0] == start
    assert intervals[-1][1] == start + timedelta(hours=24)
    assert all(end - begin == timedelta(minutes=15) for begin, end in intervals)


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
    scenarios = create_scenarios(forecast, config.scenarios)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser_specific=replace(config.energy_plan.optimiser_specific, dynamic_programing=replace(config.energy_plan.optimiser_specific.dynamic_programing, visible_timestep_minutes=30))))
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


def test_forced_first_command_is_shared_and_selected_using_all_nine_scenarios(inputs, monkeypatch):
    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_max_absorption_power_kw=1.)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=1.), *base.solar[1:]], base.load)
    scenarios = create_scenarios(forecast, config.scenarios)
    tariff = config.tariffs.periods[0]
    rule = tariff.import_[0]
    tariff = replace(tariff, import_=(replace(rule, end_time="00:30", rate=.01), replace(rule, start_time="00:30", rate=.4)))
    config = replace(config, timezone="UTC", tariffs=replace(config.tariffs, periods=(tariff,)))
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser_specific=replace(config.energy_plan.optimiser_specific, dynamic_programing=replace(config.energy_plan.optimiser_specific.dynamic_programing, visible_timestep_minutes=30))))
    original = dp.forward_projection
    records = []

    def record_forward_projection(*args):
        path = original(*args)
        records.append((args[-1], args[2], path))
        return path

    monkeypatch.setattr(dp, "forward_projection", record_forward_projection)
    result = dp.solve_pass(device, state, scenarios, config, start)
    assert result.next_action.mode == OperatingMode.CHARGE_PV_FIRST
    chosen = (result.next_action.mode, result.next_action.force_battery_charge_rate)
    selected = [record for record in records if record[0] == chosen]
    assert len(selected) == 9
    assert len({id(periods) for action, periods, path in selected}) == 9
    assert len({(periods[0].solar, periods[0].load) for action, periods, path in selected}) == 9
    assert len({round(path[0].battery_end, 6) for action, periods, path in selected}) > 1

    terminal_rate = dp.terminal_rate(config, selected[0][1][-1].ends_at) * device.discharge_efficiency
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
