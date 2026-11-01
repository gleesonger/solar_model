"""UI YAML replacement and explicitly requested configuration writes."""
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from solar_model.config import TariffRule, WEEKDAYS, energy_plan_yaml, load_config, parse_energy_plan_yaml, save_energy_plan_yaml


def test_overlay_round_trip_and_validation_leave_global_unchanged():
    config = load_config()
    text = energy_plan_yaml(config.energy_plan)
    assert "15*96" in text
    assert parse_energy_plan_yaml(config, text) == config.energy_plan
    override = parse_energy_plan_yaml(config, text.replace("protection_preference: 50", "protection_preference: 123"))
    assert override.protection_preference == 123
    assert config.energy_plan.protection_preference == 50


@pytest.mark.parametrize("text", ["[", "null", "[]", "protection_preferenc: 50", "timezone: UTC"])
def test_invalid_or_unknown_editor_settings_are_rejected(text):
    with pytest.raises(ValueError):
        parse_energy_plan_yaml(load_config(), text)


def test_overlay_uses_existing_scenario_and_projection_validation():
    config = load_config()
    text = energy_plan_yaml(config.energy_plan)
    with pytest.raises(ValueError, match="non-negative"):
        parse_energy_plan_yaml(config, text.replace("protection_preference: 50", "protection_preference: -1"))
    with pytest.raises(ValueError, match="projection"):
        parse_energy_plan_yaml(config, text.replace("15*96", "15*0"))


def test_save_replaces_only_energy_plan_and_keeps_other_text(tmp_path):
    config = load_config()
    path = tmp_path / "custom.yaml"
    original = Path("config.yaml").read_text(encoding="utf-8")
    # Put another section after energy_plan to check both sides of replacement.
    original += "\n# Keep this trailing section comment\nfuture_setting: unchanged # preserve me\n"
    path.write_text(original, encoding="utf-8")
    text = "# My UI experiment\n" + energy_plan_yaml(replace(config.energy_plan, protection_preference=123.))
    save_energy_plan_yaml(path, config, text)
    saved = path.read_text(encoding="utf-8")
    assert saved[:saved.index("energy_plan:")] == original[:original.index("energy_plan:")]
    assert "# Keep this trailing section comment\nfuture_setting: unchanged # preserve me" in saved
    assert "# My UI experiment" in saved
    assert load_config(path).energy_plan.protection_preference == 123
    assert config.energy_plan.protection_preference == 50
    assert sorted(p.name for p in tmp_path.iterdir()) == ["custom.yaml"]


def test_invalid_save_leaves_file_unchanged(tmp_path):
    config = load_config()
    path = tmp_path / "custom.yaml"
    original = Path("config.yaml").read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        save_energy_plan_yaml(path, config, "unknown: 3")
    assert path.read_text(encoding="utf-8") == original


def test_source_yaml_and_formatted_yaml_deserialise_to_the_same_settings():
    config = load_config()
    source = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))["energy_plan"]
    settings = parse_energy_plan_yaml(config, yaml.safe_dump(source))
    assert settings == config.energy_plan
    text = energy_plan_yaml(settings)
    assert "low: {multiplier:" in text
    assert "refinement_stages:\n      - {grid_step_percent:" in text
    assert "days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]" in text
    assert 'start_time: "00:00"' in text
    assert 'end_time: "24:00"' in text
    assert "name: null" not in text
    assert "\n\nscenarios:" in text
    assert parse_energy_plan_yaml(config, text) == settings
    assert energy_plan_yaml(parse_energy_plan_yaml(config, text)) == text


def test_projection_runs_remain_separate_and_singletons_stay_numeric():
    config = load_config()
    resolutions = (15,) * 8 + (30,) + (60,) * 41 + (15,) * 18
    models = config.energy_plan.optimiser
    settings = replace(config.energy_plan, optimiser=replace(models,
        mix_integer_linear_prog=replace(models.mix_integer_linear_prog, projection_resolution_mins=resolutions),
        dynamic_programming=replace(models.dynamic_programming, projection_resolution_mins=resolutions)))
    text = energy_plan_yaml(settings)
    assert text.count("projection_resolution_mins: [15*8, 30, 60*41, 15*18]") == 2
    assert parse_energy_plan_yaml(config, text) == settings


@pytest.mark.parametrize("field", ["minimum_battery_kwh", "maximum_battery_kwh", "limit_battery_export_rate_to_grid_kw"])
def test_scheduled_rules_collapse_matching_days_without_changing_windows(field):
    config = load_config()
    base = 0. if field == "minimum_battery_kwh" else 20.
    rules = []
    for index, day in enumerate(WEEKDAYS):
        rules.append(TariffRule((day,), "22:00", "06:00", base, "Night"))
        rules.append(TariffRule((day,), "06:00", "22:00", base + 1 if index < 5 else base + 2, "Day"))
    settings = replace(config.energy_plan, **{field: tuple(rules)})
    text = energy_plan_yaml(settings)
    compact = yaml.safe_load(text)[field]
    assert len(compact) == 3
    assert compact[0]["days"] == list(WEEKDAYS)
    assert compact[1]["days"] == list(WEEKDAYS[:5])
    assert compact[2]["days"] == list(WEEKDAYS[5:])
    restored = parse_energy_plan_yaml(config, text)

    def expand(schedule):
        return sorted((day, rule.start_time, rule.end_time, rule.rate, rule.name) for rule in schedule for day in rule.days)

    assert expand(getattr(restored, field)) == expand(rules)
    assert energy_plan_yaml(restored) == text


def test_rule_names_are_preserved_when_other_parameters_match():
    config = load_config()
    rules = (TariffRule(WEEKDAYS[:5], "00:00", "24:00", 0., "Weekday"), TariffRule(WEEKDAYS[5:], "00:00", "24:00", 0., "Weekend"))
    settings = replace(config.energy_plan, minimum_battery_kwh=rules)
    text = energy_plan_yaml(settings)
    assert len(yaml.safe_load(text)["minimum_battery_kwh"]) == 2
    assert parse_energy_plan_yaml(config, text) == settings


def test_empty_optional_schedules_round_trip_as_empty_lists():
    config = load_config()
    settings = replace(config.energy_plan, minimum_battery_kwh=(), maximum_battery_kwh=(), limit_battery_export_rate_to_grid_kw=())
    text = energy_plan_yaml(settings)
    assert "minimum_battery_kwh: []" in text
    assert "maximum_battery_kwh: []" in text
    assert "limit_battery_export_rate_to_grid_kw: []" in text
    assert parse_energy_plan_yaml(config, text) == settings


@pytest.mark.parametrize("model", ["mix_integer_linear_prog", "dynamic_programming"])
def test_model_round_trip_always_includes_allowed_values_comment(model):
    config = load_config()
    settings = replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, model=model))
    text = energy_plan_yaml(settings)
    assert f"  # model - mix_integer_linear_prog, dynamic_programming\n  model: {model}" in text
    assert parse_energy_plan_yaml(config, text) == settings
    assert energy_plan_yaml(parse_energy_plan_yaml(config, text)) == text


@pytest.mark.parametrize("model", ["MILP", "DP", "unknown", None, 1])
def test_unknown_model_is_rejected(model):
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["optimiser"]["model"] = model
    with pytest.raises(ValueError):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


def test_legacy_optimiser_specific_is_rejected(tmp_path):
    data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    data["energy_plan"]["optimiser_specific"] = data["energy_plan"].pop("optimiser")
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="rename energy_plan.optimiser_specific"):
        load_config(path)
