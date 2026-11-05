"""Shared first-step action variables with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cvxpy as cp
import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning import plan_optimisation_milp as optimisation
from solar_model.planning.plan_optimisation_milp import simulate_energy_plan
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, NextAction, OperatingMode, ScenarioData


def make_action(charge, discharge, charge_soc, discharge_soc):
    mode = (OperatingMode.CHARGE_PV_FIRST if charge > 0 else
            OperatingMode.DISCHARGE_PV_FIRST if discharge > 0 else OperatingMode.SELF_CONSUMPTION)
    return NextAction(mode, charge, discharge)


@pytest.fixture
def inputs():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
        inverter_max_active_power_kw=4., inverter_max_absorption_power_kw=4.,
        inverter_battery_charge_power_kw=4., inverter_battery_discharge_power_kw=4.,
        battery_charge_efficiency=1., battery_discharge_efficiency=1., grid_max_import_power_kw=float("inf"))
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", 0., "flat")
    config = load_config()
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01",
        (replace(rule, rate=.4),), (replace(rule, rate=.1),)),)),
        energy_plan=replace(config.energy_plan, num_projection_hours=25, num_visible_projection_hours=1, optimiser=replace(config.energy_plan.optimiser, mix_integer_linear_prog=replace(config.energy_plan.optimiser.mix_integer_linear_prog, projection_resolution_mins=(30, 30) + (240,) * 6)),
            minimum_battery_kwh=(rule,), maximum_battery_kwh=(replace(rule, rate=10.),),
            limit_battery_export_rate_to_grid_kw=(), protection_preference=0.))
    dp_settings = replace(config.energy_plan.optimiser.dynamic_programming, projection_resolution_mins=(15,) * 4 + (240,) * 6)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, dynamic_programming=dp_settings)))
    scenarios = [ScenarioData("base", ForecastData([ForecastInterval(start, end, 0.), ForecastInterval(end, end + timedelta(hours=24), 0.)], [ForecastInterval(start, end, 1.), ForecastInterval(end, end + timedelta(hours=24), 0.)]), 1.)]
    return device, DeviceState(2.), scenarios, config, start


def simulate(inputs, action=None, extra_constraints=None, *, return_result=False):
    original_score = optimisation.score_simulation

    def score_with_constraints(costs, levels, scenarios, device, config, constraints,
                               projection_starts_at, durations):
        variables = {variable.name(): variable for constraint in constraints
                     for variable in constraint.variables()}
        if action is not None:
            index = (0 if action.mode == OperatingMode.SELF_CONSUMPTION else
                     (1 if action.charge_power_limit_kw > 0 else 2) if action.mode == OperatingMode.CHARGE_PV_FIRST else
                     (3 if action.discharge_power_limit_kw > 0 else 4))
            constraints.extend([
                variables["next_mode"][index] == 1,
                variables["next_force_battery_charge_rate"] == action.force_battery_charge_rate,
                variables["next_force_battery_discharge_rate"] == action.force_battery_discharge_rate,
            ])
        if extra_constraints is not None:
            constraints.extend(extra_constraints(variables))
        return original_score(costs, levels, scenarios, device, config, constraints,
                              projection_starts_at, durations)

    with patch.object(optimisation, "score_simulation", score_with_constraints):
        result = simulate_energy_plan(*inputs)
        return result if return_result else result.results


def solve(inputs, action=None):
    return simulate(inputs, action)["base"]


@pytest.mark.parametrize("mode_index,field,minimum", [
    (1, "force_battery_charge_rate", .7),
    (3, "force_battery_discharge_rate", 1.2),
])
def test_forced_modes_respect_configured_minimum_power(inputs, mode_index, field, minimum):
    device, state, scenarios, config, start = inputs
    settings = replace(config.energy_plan, min_forced_charge_power_kw=.7, min_forced_discharge_power_kw=1.2, max_forced_charge_power_kw=.9, max_forced_discharge_power_kw=1.5)
    config = replace(config, energy_plan=settings)
    result = simulate(
        (device, state, scenarios, config, start),
        extra_constraints=lambda variables: [variables["next_mode"][mode_index] == 1],
        return_result=True,
    )
    assert getattr(result.next_action, field) >= minimum - 1e-6
    assert getattr(result.next_action, field) <= (.9 if mode_index == 1 else 1.5) + 1e-6


@pytest.mark.parametrize("charge_rate,discharge_rate,expected_energy", [
    (2., 0., 3.), (0., 1., 1.5), (0., 0., 1.5),
])
def test_rates_define_first_step_behaviour(inputs, charge_rate, discharge_rate, expected_energy):
    frame = solve(inputs, make_action(charge_rate, discharge_rate, 100., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)
    if charge_rate:
        assert frame.iloc[0].grid_import_kwh == pytest.approx(1.5)



@pytest.mark.parametrize("charge,discharge,expected_energy", [(4., 0., 4.), (0., 4., 0.)])
def test_controller_target_does_not_clip_first_command(inputs, charge, discharge, expected_energy):
    result = simulate(inputs, make_action(charge, discharge, 10., 90.), return_result=True)
    assert result.results["base"].iloc[0].battery_end_kwh == pytest.approx(expected_energy)
    assert result.next_action.target_soc_percent == pytest.approx(expected_energy * 10)


@pytest.mark.parametrize("mode,expected_charge", [(OperatingMode.CHARGE_PV_FIRST, 0.), (OperatingMode.DISCHARGE_PV_FIRST, .5)])
def test_zero_rate_variations_have_distinct_solar_response(inputs, mode, expected_charge):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(base.solar[0], kwh=2.), *base.solar[1:]], base.load)
    result = simulate((device, state, [ScenarioData("base", sunny, 1.)], config, start), NextAction(mode, 0., 0.), return_result=True)
    first = result.results["base"].iloc[0]
    assert result.next_action.mode == mode
    assert first.battery_end_kwh == pytest.approx(2. + expected_charge)
    assert first.battery_to_inverter_kwh == pytest.approx(0.)


def test_zero_rates_ignore_both_soc_cutoffs(inputs):
    frame = solve(inputs, make_action(0., 0., 10., 90.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(1.5)


def test_zero_rates_allow_passive_solar_charge_above_soc_cutoff(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=2.) for interval in base.solar], base.load)
    frames = simulate((device, state, [ScenarioData("base", sunny, 1.)], config, start),
        make_action(0., 0., 10., 90.))
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(2.5)


@pytest.mark.parametrize("charge_rate,discharge_rate", [(1., 1.), (5., 0.)])
def test_opposite_forcing_or_excessive_rate_is_infeasible(inputs, charge_rate, discharge_rate):
    with pytest.raises(ValueError, match="feasible energy plan"):
        simulate(inputs, extra_constraints=lambda variables: [
            variables["next_force_battery_charge_rate"] == charge_rate,
            variables["next_force_battery_discharge_rate"] == discharge_rate,
        ])


@pytest.mark.parametrize("time_step", [0, 1])
def test_shared_forcing_is_only_used_at_first_step(inputs, time_step):
    device, state, scenarios, config, start = inputs
    scenarios = [scenarios[0], ScenarioData("other", scenarios[0].forecast_data, .5)]
    def run():
        return simulate((device, state, scenarios, config, start), extra_constraints=lambda variables: [
            variables["battery_charge_input"][time_step, 0] >= .1,
            variables["battery_discharge_output"][time_step, 1] >= .1])
    if time_step == 0:
        with pytest.raises(ValueError, match="feasible energy plan"):
            run()
    else:
        run()


@pytest.mark.parametrize("charge_rate,discharge_rate,expected_energy", [(2., 0., 2.8), (0., 1., 2. - .5 / .9)])
def test_forced_rates_include_efficiency_losses(inputs, charge_rate, discharge_rate, expected_energy):
    device, state, scenarios, config, start = inputs
    device = replace(device, battery_charge_efficiency=.8, battery_discharge_efficiency=.9)
    frame = solve((device, state, scenarios, config, start), make_action(charge_rate, discharge_rate, 100., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)


def test_shared_charge_target_allows_scenario_specific_grid_import(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=1.) for interval in base.solar], base.load)
    scenarios = [scenarios[0], ScenarioData("sunny", sunny, .5)]
    frames = simulate((device, state, scenarios, config, start), make_action(2., 0., 100., 0.))
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(3.)
    assert frames["sunny"].iloc[0].battery_end_kwh == pytest.approx(3.)
    assert frames["base"].iloc[0].grid_import_kwh == pytest.approx(1.5)
    assert frames["sunny"].iloc[0].grid_import_kwh == pytest.approx(1.)


def test_forced_charge_allocates_solar_to_house_before_battery(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(base.solar[0], kwh=1.) , *base.solar[1:]], base.load)
    frame = solve((device, state, [ScenarioData("base", sunny, 1.)], config, start), make_action(2., 0., 100., 0.))
    first = frame.iloc[0]
    assert first.solar_to_load_kwh == pytest.approx(.5)
    assert first.solar_to_battery_kwh == pytest.approx(0.)
    assert first.grid_to_battery_kwh == pytest.approx(1.)


@pytest.mark.parametrize("solar_kw", [8.8, 4.])
def test_solar_and_mixed_charging_can_exceed_ac_absorption(inputs, solar_kw):
    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_battery_charge_power_kw=8.8,
                     inverter_max_active_power_kw=5.5, inverter_max_absorption_power_kw=5.5,
                     battery_charge_efficiency=.95)
    forecast = scenarios[0].forecast_data
    forecast = ForecastData(
        [replace(forecast.solar[0], kwh=solar_kw), *forecast.solar[1:]],
        [replace(interval, kwh=0.) for interval in forecast.load],
    )
    frame = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(8.8, 0., 100., 0.))
    first = frame.iloc[0]
    assert first.battery_end_kwh - first.battery_start_kwh == pytest.approx(8.8 * .5 * .95)
    assert first.solar_to_battery_kwh == pytest.approx(solar_kw * .5 * .95)
    assert first.grid_to_battery_kwh / .95 == pytest.approx((8.8 - solar_kw) * .5)
    assert first.inverter_ac_to_dc_kwh <= 5.5 * .5 + 1e-6


@pytest.mark.parametrize("charge_rate", [5.5, 8.8])
def test_grid_only_charging_still_observes_ac_absorption(inputs, charge_rate):
    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_battery_charge_power_kw=8.8,
                     inverter_max_active_power_kw=5.5, inverter_max_absorption_power_kw=5.5,
                     battery_charge_efficiency=.95)
    inputs = (device, state, scenarios, config, start)
    action = make_action(charge_rate, 0., 100., 0.)
    first = solve(inputs, action).iloc[0]
    assert first.inverter_ac_to_dc_kwh == pytest.approx(5.5 * .5)
    assert first.battery_end_kwh - first.battery_start_kwh == pytest.approx(5.5 * .5 * .95)


@pytest.mark.parametrize("initial,solar,preference,expected", [
    (2., 2., 0., (.5, .5, 0., 0., 0., 2.5)),
    (10., 2., 0., (.5, 0., 0., 0., .5, 10.)),
    (2., 0., 10000., (0., 0., .5, 0., 0., 1.5)),
    (0., 0., 0., (0., 0., 0., .5, 0., 0.)),
])
def test_self_consumption_follows_physical_response(inputs, initial, solar, preference, expected):
    device, state, scenarios, config, start = inputs
    forecast = scenarios[0].forecast_data
    forecast = ForecastData(
        [replace(interval, kwh=solar if i == 0 else 0.)
         for i, interval in enumerate(forecast.solar)], forecast.load)
    config = replace(config, energy_plan=replace(config.energy_plan, protection_preference=preference))
    frame = solve((device, DeviceState(initial), [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 0., 100., 0.))
    columns = ["solar_to_load_kwh", "solar_to_battery_kwh", "battery_to_load_kwh",
               "grid_to_load_kwh", "solar_to_grid_kwh", "battery_end_kwh"]
    assert frame.iloc[0][columns].astype(float).tolist() == pytest.approx(expected)


def test_self_consumption_keeps_dc_charging_separate_from_ac_output(inputs):
    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_max_active_power_kw=.2)
    forecast = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=2. if i == 0 else 0.)
                          for i, interval in enumerate(forecast.solar)], forecast.load)
    frame = solve((device, state, [ScenarioData("base", sunny, 1.)], config, start),
                  make_action(0., 0., 100., 0.))
    first = frame.iloc[0]
    assert first.solar_to_load_kwh == pytest.approx(.1)
    assert first.grid_to_load_kwh == pytest.approx(.4)
    assert first.solar_to_battery_kwh == pytest.approx(.9)
    assert first.battery_to_load_kwh == pytest.approx(0.)


def test_self_consumption_cannot_export_pv_by_discharging_battery_to_load(inputs):
    device, state, scenarios, config, _ = inputs
    start = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    forecast = ForecastData(
        [ForecastInterval(start, end, 2.), ForecastInterval(end, end + timedelta(hours=24), 0.)],
        [ForecastInterval(start, end, 1.), ForecastInterval(end, end + timedelta(hours=24), 0.)])
    config = replace(config, tariffs=load_config().tariffs)
    frame = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 0., 100., 0.))
    first = frame.iloc[0]
    assert first.battery_to_load_kwh == pytest.approx(0.)
    assert first.solar_to_load_kwh == pytest.approx(.5)
    assert first.solar_to_battery_kwh == pytest.approx(.5)
    assert first.solar_to_grid_kwh == pytest.approx(0.)


@pytest.mark.parametrize("solar,expected_energy", [(2., 2.4), (0., 2. - .5 / .9)])
def test_self_consumption_accounts_for_conversion_losses(inputs, solar, expected_energy):
    device, state, scenarios, config, start = inputs
    device = replace(device, battery_charge_efficiency=.8, battery_discharge_efficiency=.9)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar), *base.solar[1:]], base.load)
    first = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 0., 100., 0.)).iloc[0]
    assert first.battery_end_kwh == pytest.approx(expected_energy)


def test_shared_self_consumption_can_charge_and_discharge_in_different_scenarios(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(base.solar[0], kwh=2.), *base.solar[1:]], base.load)
    frames = simulate((device, state, [scenarios[0], ScenarioData("sunny", sunny, .5)], config, start),
                      make_action(0., 0., 100., 0.))
    assert frames["base"].iloc[0].battery_to_load_kwh == pytest.approx(.5)
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(1.5)
    assert frames["sunny"].iloc[0].solar_to_battery_kwh == pytest.approx(.5)
    assert frames["sunny"].iloc[0].battery_end_kwh == pytest.approx(2.5)


@pytest.mark.parametrize("solar", [0., 2.])
def test_self_consumption_is_rejected_when_user_bounds_require_extra_control(inputs, solar):
    device, state, scenarios, config, start = inputs
    device = replace(device, battery_charge_efficiency=.8, battery_discharge_efficiency=.9)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar), *base.solar[1:]], base.load)
    settings = config.energy_plan
    config = replace(config, energy_plan=replace(settings,
        minimum_battery_kwh=(replace(settings.minimum_battery_kwh[0], rate=1.8),),
        maximum_battery_kwh=(replace(settings.maximum_battery_kwh[0], rate=2.2),)))
    with pytest.raises(ValueError, match="feasible energy plan"):
        solve((device, state, [ScenarioData("base", forecast, 1.)], config, start), make_action(0., 0., 100., 0.))


def test_self_consumption_clips_only_surplus_beyond_inverter_capacity(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=20.), *base.solar[1:]], base.load)
    first = solve((device, DeviceState(10.), [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 0., 100., 0.)).iloc[0]
    assert first.solar_to_load_kwh == pytest.approx(.5)
    assert first.solar_to_grid_kwh == pytest.approx(1.5)
    assert first.solar_clipped_kwh == pytest.approx(8.)
    assert first.battery_end_kwh == pytest.approx(10.)


def test_economic_solar_export_with_battery_discharge_requires_forced_mode(inputs):
    device, state, scenarios, config, _ = inputs
    start = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    forecast = ForecastData(
        [ForecastInterval(start, end, 2.), ForecastInterval(end, end + timedelta(hours=24), 0.)],
        [ForecastInterval(start, end, 1.), ForecastInterval(end, end + timedelta(hours=24), 0.)])
    # Only the first half-hour earns export revenue: delaying discharge cannot
    # tie the immediate export action as it can under a flat export tariff.
    rule = config.energy_plan.minimum_battery_kwh[0]
    exports = (replace(rule, start_time="00:00", end_time="12:00", rate=0.),
               replace(rule, start_time="12:00", end_time="12:30", rate=.8),
               replace(rule, start_time="12:30", end_time="24:00", rate=0.))
    config = replace(config, tariffs=TariffsConfig((TariffPeriod(
        "2000-01-01", (replace(rule, rate=.4),), exports),)))
    result = simulate_energy_plan(device, state, [ScenarioData("base", forecast, 1.)], config, start)
    assert result.next_action.mode == OperatingMode.DISCHARGE_PV_FIRST
    assert result.next_action.force_battery_discharge_rate > 0.
    first = result.results["base"].iloc[0]
    assert first.battery_end_kwh < first.battery_start_kwh
    assert first.grid_export_kwh > 0.


@pytest.mark.parametrize("solar_kw,expected_discharge_kw", [(0., 4.), (3., 2.5), (5., .5), (8., 0.)])
def test_forced_discharge_preserves_pv_before_battery_at_inverter_limit(inputs, solar_kw, expected_discharge_kw):
    device, state, scenarios, config, start = inputs
    state = DeviceState(5.)
    device = replace(device, inverter_max_active_power_kw=5.5,
                     inverter_battery_discharge_power_kw=9.6, battery_discharge_efficiency=.95)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar_kw), *base.solar[1:]], base.load)
    first = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 4., 100., 0.)).iloc[0]
    assert first.battery_to_inverter_kwh == pytest.approx(expected_discharge_kw * .5)
    assert first.solar_to_inverter_kwh == pytest.approx(min(solar_kw, 5.5) * .5)
    assert first.solar_clipped_kwh == pytest.approx(max(0., solar_kw - 5.5) * .5)
    assert first.battery_end_kwh == pytest.approx(5. - expected_discharge_kw * .5 / .95)


def test_one_charge_command_saturates_differently_across_nine_scenarios(inputs):
    from solar_model.planning.scenarios import create_scenarios

    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_max_active_power_kw=5.5, inverter_max_absorption_power_kw=5.5,
                     inverter_battery_charge_power_kw=8.8, battery_charge_efficiency=.95)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=4.), *base.solar[1:]], base.load)
    scenarios = create_scenarios(forecast, config.energy_plan.scenarios)
    action = make_action(8.8, 0., 100., 0.)
    result = simulate((device, state, scenarios, config, start), action, return_result=True)
    assert result.next_action.mode == action.mode
    assert result.next_action.force_battery_charge_rate == pytest.approx(8.8)
    assert result.next_action.target_soc_percent is not None
    assert len(result.results) == 9
    starts = [frame.battery_start_kwh.iloc[0] for frame in result.results.values()]
    assert starts == pytest.approx([2.] * 9)
    for scenario in scenarios:
        first = result.results[scenario.name].iloc[0]
        pv = scenario.forecast_data.sum(first.starts_at, first.ends_at).solar_kwh
        achieved = min(8.8 * .5, max(0., pv - first.load_kwh) + 5.5 * .5)
        assert first.battery_end_kwh == pytest.approx(2. + achieved * .95)
        assert first.inverter_ac_to_dc_kwh <= 5.5 * .5 + 1e-6
    assert result.results["low_low"].battery_end_kwh.iloc[0] < result.results["med"].battery_end_kwh.iloc[0]


@pytest.mark.parametrize("solar,discharge_soc", [(0., 0.), (0., 15.), (2., 0.)])
def test_battery_export_cap_rejects_unsafe_rate_even_with_an_early_soc_stop(inputs, solar, discharge_soc):
    device, state, scenarios, config, start = inputs
    config = replace(config, energy_plan=replace(config.energy_plan,
        limit_battery_export_rate_to_grid_kw=(replace(config.energy_plan.minimum_battery_kwh[0], rate=0.),)))
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar), *base.solar[1:]], base.load)
    with pytest.raises(ValueError, match="feasible energy plan"):
        solve((device, state, [ScenarioData("base", forecast, 1.)], config, start), make_action(0., 4., 100., discharge_soc))


def test_zero_battery_export_cap_allows_solar_export_when_pv_fills_inverter(inputs):
    device, state, scenarios, config, start = inputs
    rule = config.energy_plan.minimum_battery_kwh[0]
    config = replace(config, energy_plan=replace(config.energy_plan, limit_battery_export_rate_to_grid_kw=(replace(rule, rate=0.),)))
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=6.), *base.solar[1:]], base.load)
    first = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start), make_action(0., 4., 100., 0.)).iloc[0]
    assert first.battery_to_inverter_kwh == pytest.approx(0.)
    assert first.solar_to_grid_kwh == pytest.approx(1.5)


@pytest.mark.parametrize("charge,discharge,ceiling,floor,limited_charge,limited_discharge,expected", [
    (4., 0., 2.5, 0., 1., 0., 2.5), (0., 4., 10., 1.5, 0., 1., 1.5),
])
def test_user_energy_bounds_require_an_executable_rate(inputs, charge, discharge, ceiling, floor, limited_charge, limited_discharge, expected):
    device, state, scenarios, config, start = inputs
    rule = config.energy_plan.minimum_battery_kwh[0]
    config = replace(config, energy_plan=replace(config.energy_plan,
        minimum_battery_kwh=(replace(rule, rate=floor),), maximum_battery_kwh=(replace(rule, rate=ceiling),)))
    inputs = (device, state, scenarios, config, start)
    with pytest.raises(ValueError, match="feasible energy plan"):
        solve(inputs, make_action(charge, discharge, 100., 0.))
    result = simulate(inputs, make_action(limited_charge, limited_discharge, 0., 0.), return_result=True)
    first = result.results["base"].iloc[0]
    assert first.battery_end_kwh == pytest.approx(expected)
    assert result.next_action.target_soc_percent == pytest.approx(expected * 10)


@pytest.mark.parametrize("solar,expected_battery_export,expected_solar_export", [(0., .5, 0.), (2., .5, .5)])
def test_battery_export_cap_is_not_a_total_site_export_cap(
        inputs, solar, expected_battery_export, expected_solar_export):
    device, state, scenarios, config, start = inputs
    rule = config.energy_plan.minimum_battery_kwh[0]
    config = replace(config, energy_plan=replace(config.energy_plan,
        limit_battery_export_rate_to_grid_kw=(replace(rule, rate=1.),)))
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar), *base.solar[1:]], base.load)
    result = simulate((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                      make_action(0., 2. if solar == 0. else 1., 100., 0.), return_result=True)
    first = result.results["base"].iloc[0]
    assert first.battery_to_grid_kwh == pytest.approx(expected_battery_export)
    assert first.solar_to_grid_kwh == pytest.approx(expected_solar_export)
    for frame in [*result.results.values(), *result.lookahead_results.values()]:
        durations = [
            (end - start).total_seconds() / 3600
            for start, end in zip(frame.starts_at, frame.ends_at)
        ]
        assert all(export <= hours + 1e-6 for export, hours in zip(frame.battery_to_grid_kwh, durations))


def test_solver_chooses_one_export_safe_discharge_rate_across_nine_scenarios(inputs):
    device, state, scenarios, config, start = inputs
    rule = config.energy_plan.minimum_battery_kwh[0]
    config = replace(config, energy_plan=replace(config.energy_plan, limit_battery_export_rate_to_grid_kw=(replace(rule, rate=0.),)))
    base = scenarios[0].forecast_data
    scenarios = [
        ScenarioData(str(i), ForecastData(base.solar, [replace(base.load[0], kwh=i / 5), *base.load[1:]]), 1 / 9)
        for i in range(1, 10)
    ]
    result = simulate(
        (device, state, scenarios, config, start),
        extra_constraints=lambda variables: [variables["next_mode"][3] == 1],
        return_result=True,
    )
    assert result.next_action.mode == OperatingMode.DISCHARGE_PV_FIRST
    assert result.next_action.force_battery_discharge_rate == pytest.approx(.2)
    for frame in result.results.values():
        first = frame.iloc[0]
        assert first.battery_to_inverter_kwh == pytest.approx(.1)
        assert first.battery_to_grid_kwh == pytest.approx(0., abs=1e-6)
