"""Exact nonlinear protection curve and configured preference validation."""
from dataclasses import replace
import pytest
from solar_model.config import load_config, validate_config
from solar_model.planning.plan_optimisation_dynamic_programming import battery_protection_score

@pytest.mark.parametrize("multiple,expected", [(0., 0.), (.5, 100 * (1 - .3 ** .5)),
    (1., 70.), (2., 80.), (3., 85.), (5., 90.), (10., 70 + 270 / 11)])
def test_agreed_reserve_curve(multiple, expected):
    assert battery_protection_score(4 * multiple, 4.) == pytest.approx(expected)


@pytest.mark.parametrize("preference", [-1., float("nan"), float("inf")])
def test_preference_validation(preference):
    config = load_config()
    config = replace(config, energy_plan=replace(config.energy_plan, protection_preference=preference))
    with pytest.raises(ValueError, match="protection_preference"):
        validate_config(config)
