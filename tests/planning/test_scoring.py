"""Contracts for tariff-scaled cost/protection scoring and diminishing returns."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import cvxpy as cp
import numpy as np
import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config, validate_config
from solar_model.planning.optimisation import (
    PROTECTION_APPROXIMATION_ERROR, _protection_segments, _reference_import_rate, battery_protection_score, score_scenario, score_simulation,
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


def scoring_inputs(probabilities=(1.,), preference=50.):
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    interval = ForecastInterval(start, start + timedelta(hours=24), 1.)
    forecast = ForecastData([interval], [interval])
    scenarios = [ScenarioData(str(i), forecast, p) for i, p in enumerate(probabilities)]
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     battery_discharge_cut_off_soc_percent=40.)
    config = load_config()
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", .3, "flat")
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (rule,), (rule,)),)),
        energy_plan=replace(config.energy_plan, num_projection_hours=24,
                            preferred_reserve_kwh=4., protection_preference=preference))
    return scenarios, device, config, start


@pytest.mark.parametrize("preference", [0., 50., 100.])
def test_tariff_scaled_tradeoff_without_reference_solves(preference, monkeypatch):
    scenarios, device, config, start = scoring_inputs(preference=preference)
    fill = cp.Variable()
    constraints = [fill >= 0, fill <= 1]
    # Score construction must never solve a reference problem.
    with monkeypatch.context() as context:
        context.setattr(cp.Problem, "solve", lambda *a, **k: pytest.fail("reference solve"))
        score = score_simulation([[10 + .5 * fill]], [[4 * fill]], scenarios, device, config, constraints, start)
    problem = cp.Problem(cp.Maximize(score), constraints)
    problem.solve(solver=cp.HIGHS)
    assert problem.status == cp.OPTIMAL
    if preference == 0:
        assert fill.value == pytest.approx(0., abs=1e-5)
    else:
        assert 0 < fill.value < 1
        expected = -(10 + .5 * fill.value) + preference / 100 * 4 * .3 * battery_protection_score(4 * fill.value, 4) / 100
        assert problem.value == pytest.approx(expected, abs=1.2 * PROTECTION_APPROXIMATION_ERROR / 100)


def test_full_preference_still_rejects_expensive_protection():
    scenarios, device, config, start = scoring_inputs(preference=100.)
    fill = cp.Variable()
    constraints = [fill >= 0, fill <= 1]
    score = score_simulation([[10 + 2 * fill]], [[4 * fill]], scenarios, device, config, constraints, start)
    cp.Problem(cp.Maximize(score), constraints).solve(solver=cp.HIGHS)
    assert fill.value == pytest.approx(0., abs=1e-5)


def test_tiny_protection_range_is_not_expanded_to_full_scale():
    scenarios, device, config, start = scoring_inputs(preference=100.)
    choice = cp.Variable(boolean=True)
    constraints = []
    score = score_simulation([[10 + .01 * choice]], [[4 + .01 * choice]], scenarios, device, config, constraints, start)
    cp.Problem(cp.Maximize(score), constraints).solve(solver=cp.HIGHS)
    assert choice.value == pytest.approx(0.)


@pytest.mark.parametrize("probabilities,expected", [([.9, .1], 0.), ([.1, .9], 1.), ([9., 1.], 0.)])
def test_probabilities_weight_cost_and_protection(probabilities, expected):
    scenarios, device, config, start = scoring_inputs(probabilities)
    choice = cp.Variable()
    constraints = [choice >= 0, choice <= 1]
    score = score_simulation([[choice], [1 - choice]], [[cp.Constant(4.)], [cp.Constant(4.)]],
                             scenarios, device, config, constraints, start)
    problem = cp.Problem(cp.Maximize(score), constraints)
    problem.solve(solver=cp.HIGHS)
    assert choice.value == pytest.approx(expected, abs=1e-5)
    assert problem.value == pytest.approx(-.1 + .6 * .7)


@pytest.mark.parametrize("hours", [12, 24, 48])
def test_reward_uses_zero_based_energy_and_scales_with_horizon(hours):
    scenarios, device, config, start = scoring_inputs()
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=hours))
    constraints = []
    score = score_simulation([[cp.Constant(10.)]], [[cp.Constant(4.)]], scenarios, device, config, constraints, start)
    problem = cp.Problem(cp.Maximize(score), constraints)
    problem.solve(solver=cp.HIGHS)
    # Device cut-off is 4 kWh, but 4 stored kWh still scores 70, not zero.
    assert problem.value == pytest.approx(-10 + .6 * .7 * hours / 24)


@pytest.mark.parametrize("energy,expected", [(4., 70.), (8., 80.), (10., 70 + 30 * 6 / 14)])
def test_protection_diminishes_above_target(energy, expected):
    _, device, config, _ = scoring_inputs()
    constraints = []
    _, protection = score_scenario([cp.Constant(0.)], [cp.Constant(energy)], device, config, constraints)
    problem = cp.Problem(cp.Maximize(protection), constraints)
    problem.solve(solver=cp.HIGHS)
    assert expected - PROTECTION_APPROXIMATION_ERROR <= problem.value <= expected + 1e-6


@pytest.mark.parametrize("rate,expected", [(.3, .3), (0., 0.), (-.1, 0.)])
def test_flat_and_non_positive_reference_tariffs(rate, expected):
    _, _, config, start = scoring_inputs()
    period = config.tariffs.periods[0]
    config = replace(config, tariffs=TariffsConfig((replace(period, import_=(replace(period.import_[0], rate=rate),)),)))
    assert _reference_import_rate(config, start, start + timedelta(hours=24)) == pytest.approx(expected)


@pytest.mark.parametrize("start,before_hours,cheap_hours", [
    (datetime(2026, 3, 28, 12, 0, 30, tzinfo=timezone.utc), 12, 3),
    (datetime(2026, 10, 24, 12, 0, 30, tzinfo=timezone.utc), 11, 5),
])
def test_tariff_average_samples_elapsed_hours_across_dst_and_effective_dates(start, before_hours, cheap_hours):
    from zoneinfo import ZoneInfo
    _, _, config, _ = scoring_inputs()
    days = config.tariffs.periods[0].import_[0].days
    cheap = TariffRule(days, "00:00", "04:00", .1, "cheap")
    peak = TariffRule(days, "04:00", "24:00", .4, "peak")
    date = (start + timedelta(hours=24)).astimezone(ZoneInfo("Europe/Dublin")).date().isoformat()
    before = TariffPeriod("2000-01-01", (replace(cheap, end_time="24:00", rate=.2),), (peak,))
    after = TariffPeriod(date, (cheap, peak), (peak,))
    config = replace(config, timezone="Europe/Dublin", tariffs=TariffsConfig((before, after)))
    expected = (before_hours * .2 + cheap_hours * .1 + (24 - before_hours - cheap_hours) * .4) / 24
    assert _reference_import_rate(config, start, start + timedelta(hours=24)) == pytest.approx(expected)


@pytest.mark.parametrize("preference", [-1., 101., float("nan"), float("inf")])
def test_preference_validation(preference):
    _, _, config, _ = scoring_inputs(preference=preference)
    with pytest.raises(ValueError, match="protection_preference"):
        validate_config(config)


def test_reference_range_samples_exactly_24_hourly_values(monkeypatch):
    from solar_model.planning import optimisation
    _, _, config, start = scoring_inputs()
    days = config.tariffs.periods[0].import_[0].days
    night = TariffRule(days, "22:00", "06:00", .1, "night")
    day = TariffRule(days, "06:00", "22:00", .4, "day")
    config = replace(config, timezone="UTC", tariffs=TariffsConfig((TariffPeriod("2000-01-01", (night, day), (day,)),)))
    start = start.replace(hour=21, minute=30)
    calls = []
    lookup = optimisation.tariff_rule
    def tracked(rules, timestamp):
        calls.append(timestamp)
        return lookup(rules, timestamp)
    monkeypatch.setattr(optimisation, "tariff_rule", tracked)
    rate = _reference_import_rate(config, start, start + timedelta(hours=24))
    assert rate == pytest.approx((16 * .4 + 8 * .1) / 24)
    assert calls == [start + timedelta(hours=hour) for hour in range(24)]


@pytest.mark.parametrize("offset", [0, -1])
def test_reference_range_rejects_empty_or_reversed_range(offset):
    _, _, config, start = scoring_inputs()
    with pytest.raises(ValueError, match="end must be after start"):
        _reference_import_rate(config, start, start + timedelta(hours=offset))


def test_protection_is_weighted_by_interval_duration():
    _, device, config, _ = scoring_inputs()
    constraints = []
    _, protection = score_scenario([cp.Constant(0.), cp.Constant(0.)],
        [cp.Constant(4.), cp.Constant(0.)], device, config, constraints, [.25, 1.])
    problem = cp.Problem(cp.Maximize(protection), constraints)
    problem.solve(solver=cp.HIGHS)
    assert problem.value == pytest.approx(70 * .25 / 1.25)
