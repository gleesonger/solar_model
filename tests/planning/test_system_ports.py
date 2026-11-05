"""Independent conservation and scoring checks for the system-port formulation."""
from dataclasses import replace

import cvxpy as cp
import numpy as np
import pandas as pd
import pytest

from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.system import allocate_energy_routes
from solar_model.planning.types import ForecastData, ScenarioData
from tests.planning.test_next_action import inputs, make_action, simulate


def test_report_allocations_preserve_port_energy_and_net_meter_exchange(inputs):
    device = replace(inputs[0], inverter_ac_to_dc_efficiency=.9, inverter_dc_to_ac_efficiency=.8,
                     battery_charge_efficiency=.7, battery_discharge_efficiency=.6)
    solar = np.array([3., 3., 0.])
    used = np.array([3., 2., 0.])
    load = np.array([1., 2., 1.])
    charge = np.array([1., 0., .9])
    grid_charge = np.array([0., 0., 1.])
    discharge = np.array([0., 1., 0.])
    flows = allocate_energy_routes(solar, load, used, charge, grid_charge, discharge, device, [1.] * len(solar))
    # Lists are the reporting contract; arrays here independently check balances.
    assert all(isinstance(values, list) for values in flows.values())
    flows = {name: np.asarray(values) for name, values in flows.items()}
    assert all((value >= 0.).all() for value in flows.values())
    np.testing.assert_allclose(solar, (flows["solar_to_load"] + flows["solar_to_grid"]) / .8
                               + flows["solar_to_battery"] / .7 + flows["solar_clipped"])
    np.testing.assert_allclose(load, flows["solar_to_load"] + flows["battery_to_load"] + flows["grid_to_load"])
    np.testing.assert_allclose(.7 * charge - discharge / .6,
        flows["solar_to_battery"] + flows["grid_to_battery"]
        - (flows["battery_to_load"] + flows["battery_to_grid"]) / (.8 * .6))
    imports = flows["grid_to_load"] + grid_charge
    exports = flows["solar_to_grid"] + flows["battery_to_grid"]
    np.testing.assert_allclose(exports - imports, .8 * (used - charge + .9 * grid_charge + discharge) - grid_charge - load)
    total_loss = sum(flows[name] for name in ("battery_charge_loss", "battery_discharge_loss", "inverter_ac_to_dc_loss", "inverter_dc_to_ac_loss"))
    np.testing.assert_allclose(solar + imports + discharge / .6 - load - exports - charge * .7 - flows["solar_clipped"], total_loss)


def test_one_solve_keeps_nine_scenarios_and_the_same_weighted_objective(inputs, monkeypatch):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    forecast = ForecastData([replace(base.solar[0], kwh=4.), *base.solar[1:]], base.load)
    scenarios = create_scenarios(forecast, config.energy_plan.scenarios)
    config = replace(config, energy_plan=replace(config.energy_plan, protection_preference=50.))
    problems = []
    solve = cp.Problem.solve
    def record(problem, *args, **kwargs):
        problems.append(problem)
        return solve(problem, *args, **kwargs)
    monkeypatch.setattr(cp.Problem, "solve", record)
    result = simulate((device, state, scenarios, config, start),
                      make_action(2., 0., 100., 0.), return_result=True)
    assert len(problems) == 1
    assert len(result.results) == 9
    expected = 0.
    for scenario in scenarios:
        frame = pd.concat([result.results[scenario.name], result.lookahead_results[scenario.name]])
        np.testing.assert_allclose(frame.total_score.astype(float),
            (frame.cost_score + frame.protection_score + frame.terminal_score).astype(float))
        expected += scenario.probability * frame.total_score.sum()
    expected /= sum(s.probability for s in scenarios)
    assert expected == pytest.approx(max(problem.value for problem in problems), abs=1e-6)


@pytest.mark.parametrize("energy", [-1., float("nan"), float("inf")])
def test_invalid_forecast_energy_is_rejected_before_solving(inputs, energy):
    device, state, scenarios, config, start = inputs
    base = scenarios[0].forecast_data
    bad = ForecastData([replace(base.solar[0], kwh=energy), *base.solar[1:]], base.load)
    with pytest.raises(ValueError, match="finite|non-negative"):
        simulate((device, state, [ScenarioData("bad", bad, 1.)], config, start))
