"""Condition magnitudes are required numeric multipliers, not probabilities."""
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from solar_model.config import energy_plan_yaml, load_config, parse_energy_plan_yaml


def test_scalar_multipliers_round_trip_without_probabilities() -> None:
    config = load_config()
    text = energy_plan_yaml(config.energy_plan)
    parsed = yaml.safe_load(text)
    assert parsed["scenario_multiplier"]["solar"] == {"low": .5, "med": 1., "high": 1.5}
    assert parsed["scenario_multiplier"]["house_load"]["low"] == .7
    assert "probability" not in text and "scenarios:" not in text
    assert parse_energy_plan_yaml(config, text) == config.energy_plan


@pytest.mark.parametrize("dimension", ["solar", "house_load"])
@pytest.mark.parametrize("value", [-1., float("nan"), float("inf"), True, {"multiplier": .5, "probability": .2}])
def test_invalid_multipliers_are_rejected(dimension: str, value: object) -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["scenario_multiplier"][dimension]["low"] = value
    with pytest.raises(ValueError):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


@pytest.mark.parametrize("level,value", [("med", 0.), ("low", 2.), ("high", .8)])
def test_medium_is_positive_and_levels_are_ordered(level: str, value: float) -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["scenario_multiplier"]["solar"][level] = value
    with pytest.raises(ValueError):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


def test_old_scenarios_block_requires_explicit_migration() -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["scenarios"] = data.pop("scenario_multiplier")
    with pytest.raises(ValueError, match="rename energy_plan.scenarios"):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


def test_all_condition_values_are_required() -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    del data["scenario_multiplier"]["house_load"]["high"]
    with pytest.raises(ValueError, match="scenario_multiplier.house_load.high"):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))
