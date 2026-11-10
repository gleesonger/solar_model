"""The two retention settings load, validate and round-trip through the editor."""
from dataclasses import replace

import pytest

from solar_model.config import PlanHistoryConfig, energy_plan_yaml, load_config, parse_energy_plan_yaml, validate_config


def test_history_settings_round_trip_and_missing_section_fails():
    config = load_config()
    assert config.energy_plan.history == PlanHistoryConfig(30, 30)
    settings = replace(config.energy_plan, history=PlanHistoryConfig(60, 7))
    assert parse_energy_plan_yaml(config, energy_plan_yaml(settings)) == settings
    text = energy_plan_yaml(config.energy_plan)
    start, end = text.index("history:"), text.index("scenarios:")
    with pytest.raises(ValueError, match="energy_plan.history"):
        parse_energy_plan_yaml(config, text[:start] + text[end:])


@pytest.mark.parametrize("name", ["retain_snapshots_frequency_mins", "retain_snapshots_period_days"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_history_settings_reject_non_positive_or_non_integer_values(name, value):
    config = load_config()
    history = replace(config.energy_plan.history, **{name: value})
    with pytest.raises(ValueError, match=name):
        validate_config(replace(config, energy_plan=replace(config.energy_plan, history=history)))
