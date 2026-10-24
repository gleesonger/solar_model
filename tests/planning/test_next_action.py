"""Shared first-step action variables with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cvxpy as cp
import pytest

from solar_model.config import OptimisationWeightsConfig, TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, NextAction, ScenarioData


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
        energy_plan=replace(config.energy_plan, num_projection_hours=1, projection_resolution_mins=30,
            minimum_battery_kwh=(rule,), maximum_battery_kwh=(replace(rule, rate=10.),),
            limit_battery_export_rate_to_grid_kw=(), optimisation_weights=OptimisationWeightsConfig(1., 0.)))
    scenarios = [ScenarioData("base", ForecastData([ForecastInterval(start, end, 0.)], [ForecastInterval(start, end, 1.)]), 1.)]
    return device, DeviceState(2.), scenarios, config, start


def simulate(inputs, action=None, extra_constraints=None):
    original_init = cp.Problem.__init__
    configured = False

    def initialise_problem(problem, objective, constraints=None):
        nonlocal configured
        constraints = list(constraints or [])
        if not configured:
            variables = {variable.name(): variable for expression in [objective, *constraints]
                         for variable in expression.variables()}
            if "next_force_battery_charge_rate" in variables:
                if action is not None:
                    constraints.extend(variables["next_" + field.name] == getattr(action, field.name)
                                       for field in fields(action))
                if extra_constraints is not None:
                    constraints.extend(extra_constraints(variables))
                configured = True
        original_init(problem, objective, constraints)

    with patch.object(cp.Problem, "__init__", initialise_problem):
        return simulate_energy_plan(*inputs)


def solve(inputs, action=None):
    return simulate(inputs, action)["base"]


@pytest.mark.parametrize("charge_rate,discharge_rate,expected_energy", [
    (2., 0., 3.), (0., 1., 1.5), (0., 0., 1.5),
])
def test_rates_define_first_step_behaviour(inputs, charge_rate, discharge_rate, expected_energy):
    frame = solve(inputs, NextAction(charge_rate, discharge_rate, 100., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)
    if charge_rate:
        assert frame.iloc[0].grid_import_kwh == pytest.approx(1.5)
        assert frame.iloc[1].battery_to_grid_kwh > .5


@pytest.mark.parametrize("action,expected_energy", [
    (NextAction(4., 0., 25., 90.), 2.5),
    (NextAction(0., 4., 10., 15.), 1.5),
    (NextAction(4., 0., 10., 0.), 2.),
    (NextAction(0., 4., 100., 30.), 2.),
])
def test_forcing_stops_at_soc_including_starting_beyond_cutoff(inputs, action, expected_energy):
    frame = solve(inputs, action)
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)


def test_zero_rates_ignore_both_soc_cutoffs(inputs):
    frame = solve(inputs, NextAction(0., 0., 10., 90.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(1.5)


def test_zero_rates_allow_passive_solar_charge_above_soc_cutoff(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=2.) for interval in base.solar], base.load)
    frames = simulate((device, state, [ScenarioData("base", sunny, 1.)], config, start),
        NextAction(0., 0., 10., 90.),
        lambda variables: [variables["solar_to_load"][0, 0] == .5,
                           variables["solar_to_battery"][0, 0] == .5])
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(2.5)


@pytest.mark.parametrize("charge_rate,discharge_rate", [(1., 1.), (5., 0.)])
def test_opposite_forcing_or_excessive_rate_is_infeasible(inputs, charge_rate, discharge_rate):
    with pytest.raises(ValueError, match="feasible energy plan"):
        solve(inputs, NextAction(charge_rate, discharge_rate, 100., 0.))


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
    frame = solve((device, state, scenarios, config, start), NextAction(charge_rate, discharge_rate, 100., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(expected_energy)


def test_shared_charge_target_allows_scenario_specific_grid_import(inputs):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    sunny = ForecastData([replace(interval, kwh=1.) for interval in base.solar], base.load)
    scenarios = [scenarios[0], ScenarioData("sunny", sunny, .5)]
    frames = simulate((device, state, scenarios, config, start), NextAction(2., 0., 100., 0.))
    assert frames["base"].iloc[0].battery_end_kwh == pytest.approx(3.)
    assert frames["sunny"].iloc[0].battery_end_kwh == pytest.approx(3.)
    assert frames["base"].iloc[0].grid_import_kwh == pytest.approx(1.5)
    assert frames["sunny"].iloc[0].grid_import_kwh == pytest.approx(1.)


def test_soc_stopping_accounts_for_charge_efficiency(inputs):
    device, state, scenarios, config, start = inputs
    device = replace(device, charge_efficiency=.8)
    frame = solve((device, state, scenarios, config, start), NextAction(4., 0., 25., 0.))
    assert frame.iloc[0].battery_end_kwh == pytest.approx(2.5)
    assert frame.iloc[0].grid_to_battery_kwh + frame.iloc[0].grid_to_battery_loss_kwh == pytest.approx(.5 / .8)
