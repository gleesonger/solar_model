"""Contracts for reserve utility, approximation and reference-normalised scoring."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import cvxpy as cp
import numpy as np
import pytest

from solar_model.config import OptimisationWeightsConfig, load_config
from solar_model.planning.optimisation import (
    PROTECTION_APPROXIMATION_ERROR, _protection_segments, battery_protection_score, score_simulation,
)
from solar_model.planning.types import DeviceInfo, ForecastData, ForecastInterval, ScenarioData


@pytest.mark.parametrize("multiple,expected", [(0., 0.), (.5, 100 * (1 - .3 ** .5)),
    (1., 70.), (2., 80.), (3., 85.), (5., 90.), (10., 70 + 270 / 11)])
def test_agreed_reserve_curve(multiple, expected):
    assert battery_protection_score(4 * multiple, 4.) == pytest.approx(expected)


@pytest.mark.parametrize("capacity,reserve", [(2., 4.), (10., 4.), (40., 1.)])
def test_piecewise_score_understates_curve_within_error_bound(capacity, reserve):
    segments = _protection_segments(capacity, reserve)
    for energy in np.linspace(0., capacity, 5001):
        approximate = min(slope * energy + intercept for slope, intercept in segments)
        exact = battery_protection_score(energy, reserve)
        assert -1e-9 <= exact - approximate <= PROTECTION_APPROXIMATION_ERROR + 1e-9


def scoring_inputs(probabilities, battery_weight):
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    interval = ForecastInterval(start, start + timedelta(hours=1), 1.)
    forecast = ForecastData([interval], [interval])
    scenarios = [ScenarioData(str(i), forecast, p) for i, p in enumerate(probabilities)]
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     battery_discharge_cut_off_soc_percent=0.)
    config = load_config()
    config = replace(config, energy_plan=replace(config.energy_plan,
        preferred_reserve_kwh=4., optimisation_weights=OptimisationWeightsConfig(1 - battery_weight, battery_weight)))
    return scenarios, device, config


@pytest.mark.parametrize("battery_weight", [0., .5, 1.])
def test_reference_endpoints_and_interior_tradeoff(battery_weight):
    scenarios, device, config = scoring_inputs([1.], battery_weight)
    fill = cp.Variable()
    constraints = [fill >= 0, fill <= 1]
    score = score_simulation([[10 + 2 * fill]], [[4 * fill]], scenarios, device, config, constraints)
    problem = cp.Problem(cp.Maximize(score), constraints)
    problem.solve(solver=cp.HIGHS)
    assert problem.status == cp.OPTIMAL
    if battery_weight == 0:
        assert fill.value == pytest.approx(0., abs=1e-5)
    elif battery_weight == 1:
        assert fill.value == pytest.approx(1., abs=1e-5)
    else:
        assert 0 < fill.value < 1
        assert problem.value > 50


@pytest.mark.parametrize("probabilities,expected", [([.9, .1], 0.), ([.1, .9], 1.)])
def test_probabilities_influence_reference_cost_and_handle_zero_protection_range(probabilities, expected):
    scenarios, device, config = scoring_inputs(probabilities, .5)
    choice = cp.Variable()
    constraints = [choice >= 0, choice <= 1]
    score = score_simulation([[choice], [1 - choice]], [[cp.Constant(4.)], [cp.Constant(4.)]],
                             scenarios, device, config, constraints)
    problem = cp.Problem(cp.Maximize(score), constraints)
    problem.solve(solver=cp.HIGHS)
    assert problem.status == cp.OPTIMAL
    assert choice.value == pytest.approx(expected, abs=1e-5)
    assert problem.value == pytest.approx(100.)
