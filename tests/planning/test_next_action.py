"""Shared first-step action variables with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cvxpy as cp
import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning import optimisation
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, NextAction, OperatingMode, ScenarioData


def make_action(charge, discharge, charge_soc, discharge_soc):
    mode = (OperatingMode.CHARGE_PV_FIRST if charge > 0 else
            OperatingMode.DISCHARGE_PV_FIRST if discharge > 0 else OperatingMode.SELF_CONSUMPTION)
    return NextAction(mode, charge, discharge, charge_soc, discharge_soc)


@pytest.fixture
def inputs():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
        inverter_max_active_power_kw=4., inverter_max_absorption_power_kw=4.,
        inverter_battery_charge_power_kw=4., inverter_battery_discharge_power_kw=4.,
        charge_efficiency=1., discharge_efficiency=1.)
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", 0., "flat")
    config = load_config()
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01",
        (replace(rule, rate=.4),), (replace(rule, rate=.1),)),)),
        energy_plan=replace(config.energy_plan, num_projection_hours=1, projection_resolution_mins=(30, 30),
            minimum_battery_kwh=(rule,), maximum_battery_kwh=(replace(rule, rate=10.),),
            limit_battery_export_rate_to_grid_kw=(), protection_preference=0.))
    scenarios = [ScenarioData("base", ForecastData([ForecastInterval(start, end, 0.), ForecastInterval(end, end + timedelta(hours=24), 0.)], [ForecastInterval(start, end, 1.), ForecastInterval(end, end + timedelta(hours=24), 0.)]), 1.)]
    return device, DeviceState(2.), scenarios, config, start


def simulate(inputs, action=None, extra_constraints=None):
    original_score = optimisation.score_simulation

    def score_with_constraints(costs, levels, scenarios, device, config, constraints,
                               projection_starts_at, durations):
        variables = {variable.name(): variable for constraint in constraints
                     for variable in constraint.variables()}
        if action is not None:
            constraints.extend(variables["next_" + field.name] == getattr(action, field.name)
                               for field in fields(action) if field.name != "mode")
        if extra_constraints is not None:
            constraints.extend(extra_constraints(variables))
        return original_score(costs, levels, scenarios, device, config, constraints,
                              projection_starts_at, durations)

    with patch.object(optimisation, "score_simulation", score_with_constraints):
        return simulate_energy_plan(*inputs).results


def solve(inputs, action=None):
    return simulate(inputs, action)["base"]


@pytest.mark.parametrize("charge_rate,discharge_rate,expected_energy", [
    (2., 0., 3.), (0., 1., 1.5), (0., 0., 1.5),
])
def test_rates_define_first_step_behaviour(inputs, charge_rate, discharge_rate, expected_energy):
    frame = solve(inputs, make_action(charge_rate, discharge_rate, 100., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)
    if charge_rate:
        assert frame.iloc[0].grid_import_kwh == pytest.approx(1.5)



@pytest.mark.parametrize("action,expected_energy", [
    (make_action(4., 0., 25., 90.), 2.5),
    (make_action(0., 4., 10., 15.), 1.5),
    (make_action(4., 0., 10., 0.), 2.),
    (make_action(0., 4., 100., 30.), 2.),
])
def test_forcing_stops_at_soc_including_starting_beyond_cutoff(inputs, action, expected_energy):
    frame = solve(inputs, action)
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)


def test_zero_rates_ignore_both_soc_cutoffs(inputs):
    frame = solve(inputs, make_action(0., 0., 10., 90.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(1.5)


def test_zero_rates_allow_passive_solar_charge_above_soc_cutoff(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=2.) for interval in base.solar], base.load)
    frames = simulate((device, state, [ScenarioData("base", sunny, 1.)], config, start),
        make_action(0., 0., 10., 90.),
        lambda variables: [variables["solar_to_load"][0, 0] == .5,
                           variables["solar_to_battery"][0, 0] == .5])
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(2.5)


@pytest.mark.parametrize("charge_rate,discharge_rate", [(1., 1.), (5., 0.)])
def test_opposite_forcing_or_excessive_rate_is_infeasible(inputs, charge_rate, discharge_rate):
    with pytest.raises(ValueError, match="feasible energy plan"):
        solve(inputs, make_action(charge_rate, discharge_rate, 100., 0.))


@pytest.mark.parametrize("time_step", [0, 1])
def test_shared_forcing_is_only_used_at_first_step(inputs, time_step):
    device, state, scenarios, config, start = inputs
    scenarios = [scenarios[0], ScenarioData("other", scenarios[0].forecast_data, .5)]
    def run():
        return simulate((device, state, scenarios, config, start), extra_constraints=lambda variables: [
            variables["grid_to_battery"][time_step, 0] >= .1,
            variables["battery_to_grid"][time_step, 1] >= .1])
    if time_step == 0:
        with pytest.raises(ValueError, match="feasible energy plan"):
            run()
    else:
        run()


@pytest.mark.parametrize("charge_rate,discharge_rate,expected_energy", [(2., 0., 2.8), (0., 1., 2. - .5 / .9)])
def test_forced_rates_include_efficiency_losses(inputs, charge_rate, discharge_rate, expected_energy):
    device, state, scenarios, config, start = inputs
    device = replace(device, charge_efficiency=.8, discharge_efficiency=.9)
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


def test_soc_stopping_accounts_for_charge_efficiency(inputs):
    device, state, scenarios, config, start = inputs
    device = replace(device, charge_efficiency=.8)
    frame = solve((device, state, scenarios, config, start), make_action(4., 0., 25., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(2.5)
    assert frame.iloc[0].grid_to_battery_kwh + frame.iloc[0].grid_to_battery_loss_kwh == pytest.approx(.5 / .8)


@pytest.mark.parametrize("solar_kw", [8.8, 4.])
def test_solar_and_mixed_charging_can_exceed_ac_absorption(inputs, solar_kw):
    device, state, scenarios, config, start = inputs
    device = replace(device, inverter_battery_charge_power_kw=8.8,
                     inverter_max_active_power_kw=5.5, inverter_max_absorption_power_kw=5.5,
                     charge_efficiency=.95)
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
                     charge_efficiency=.95)
    inputs = (device, state, scenarios, config, start)
    action = make_action(charge_rate, 0., 100., 0.)
    if charge_rate > 5.5:
        with pytest.raises(ValueError, match="feasible energy plan"):
            solve(inputs, action)
    else:
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
    device = replace(device, charge_efficiency=.8, discharge_efficiency=.9)
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


@pytest.mark.parametrize("solar,expected_energy,expected_import,expected_export", [
    (0., 1.8, .32, 0.),
    (2., 2.2, 0., .25),
])
def test_self_consumption_stops_at_configured_energy_bounds(
        inputs, solar, expected_energy, expected_import, expected_export):
    device, state, scenarios, config, start = inputs
    device = replace(device, charge_efficiency=.8, discharge_efficiency=.9)
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=solar), *base.solar[1:]], base.load)
    settings = config.energy_plan
    config = replace(config, energy_plan=replace(settings,
        minimum_battery_kwh=(replace(settings.minimum_battery_kwh[0], rate=1.8),),
        maximum_battery_kwh=(replace(settings.maximum_battery_kwh[0], rate=2.2),)))
    first = solve((device, state, [ScenarioData("base", forecast, 1.)], config, start),
                  make_action(0., 0., 100., 0.)).iloc[0]
    assert first.battery_end_kwh == pytest.approx(expected_energy)
    assert first.grid_import_kwh == pytest.approx(expected_import)
    assert first.grid_export_kwh == pytest.approx(expected_export)


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
