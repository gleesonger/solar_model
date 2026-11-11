from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
import yaml

from solar_model.config import load_config


@pytest.mark.parametrize("seconds", [0., -1., float("inf"), float("nan"), True])
def test_calculation_deadline_rejects_invalid_values(seconds):
    from dataclasses import replace
    from solar_model.config import load_config, validate_config
    config = load_config()
    with pytest.raises(ValueError, match="calculation_timeout_seconds"):
        validate_config(replace(config, energy_plan=replace(config.energy_plan, calculation_timeout_seconds=seconds)))


@pytest.mark.parametrize("name,value", [("projection_resolution_mins", [15] * 96), ("dynamic_programming", {})])
def test_legacy_flat_optimiser_settings_require_explicit_migration(tmp_path, name, value):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"][name] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="move optimiser settings"):
        load_config(path)


@pytest.mark.parametrize("changes", [
    {"backend": "numba"}, {"compile_model_to_native_code": "true"}, {"compile_model_to_native_code": 1}, {"compile_model_to_native_code": None},
    {"projection_resolution_mins": 0}, {"grid_step_percent": 0.}, {"grid_step_percent": float("nan")},
    {"power_divisions": 0}, {"power_divisions": 1.5}, {"model": "dynamic_programming"},
    {"uncertainty_model": "independent_scenarios"}, {"dynamic_programming": {}}, {"mix_integer_linear_prog": {}}, {"refinement_stages": []},
])
def test_invalid_dp_settings_are_rejected(tmp_path, changes):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser"].update(changes)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="optimiser"):
        load_config(path)


def test_forced_command_preferences_are_required_and_load_separately(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for name in ("min_forced_charge_power_kw", "min_forced_discharge_power_kw", "max_forced_charge_power_kw", "max_forced_discharge_power_kw"):
        raw["energy_plan"].pop(name, None)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="min_forced_charge_power_kw"):
        load_config(path)
    raw["energy_plan"].update(min_forced_charge_power_kw=.7, min_forced_discharge_power_kw=1.2, max_forced_charge_power_kw=3., max_forced_discharge_power_kw=4.)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_config(path).energy_plan
    assert settings.min_forced_charge_power_kw == .7
    assert settings.min_forced_discharge_power_kw == 1.2
    assert settings.max_forced_charge_power_kw == 3.
    assert settings.max_forced_discharge_power_kw == 4.


@pytest.mark.parametrize("name", ["min_forced_charge_power_kw", "min_forced_discharge_power_kw", "max_forced_charge_power_kw", "max_forced_discharge_power_kw"])
@pytest.mark.parametrize("value", [0., -.1, float("nan"), float("inf")])
def test_forced_command_minimums_reject_invalid_values(tmp_path, name, value):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"][name] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=name):
        load_config(path)


def test_load_config_parses_minimum_battery_backup_rules_and_ignores_comment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    raw["dashboard"]["actuals_to_forecast"] = {"pv1": "East"}
    raw["energy_plan"]["minimum_battery_kwh"] = {
        "comment": "Retain this much energy for an outage.",
        "rules": [{"days": days, "name": None, "start_time": "00:00", "end_time": "24:00", "rate": 4.5}],
    }
    raw["energy_plan"]["maximum_battery_kwh"] = [{"days": days, "name": None, "start_time": "00:00", "end_time": "24:00", "rate": 18}]
    raw["energy_plan"]["limit_battery_export_rate_to_grid_kw"] = [{"days": days, "name": None, "start_time": "00:00", "end_time": "24:00", "rate": 5.5}]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)

    assert config.energy_plan.minimum_battery_kwh[0].rate == 4.5
    assert config.dashboard.actuals_to_forecast == {"pv1": "East"}
    assert config.actuals_to_forecast_panel_ids == {"pv1": 1}


def test_array_mapping_resolves_names_independently_of_order_and_rejects_ambiguity():
    from solar_model.config import validate_config

    config = load_config()
    east, west = config.forecast.solar.arrays
    config = replace(config, forecast=replace(config.forecast, solar=replace(config.forecast.solar, arrays=(west, east))),
                     dashboard=replace(config.dashboard, actuals_to_forecast={"pv1": west.name, "pv2": east.name}))
    validate_config(config)
    assert config.actuals_to_forecast_panel_ids == {"pv1": west.panel_id, "pv2": east.panel_id}
    with pytest.raises(ValueError, match="unknown array names"):
        validate_config(replace(config, dashboard=replace(config.dashboard, actuals_to_forecast={"pv1": "missing"})))
    with pytest.raises(ValueError, match="array names must be unique"):
        validate_config(replace(config, forecast=replace(config.forecast, solar=replace(config.forecast.solar, arrays=(east, replace(west, name=east.name))))))


@pytest.mark.parametrize("entries", [["15*24", "60*18"], [15, "15 * 23", "60*18"],
                                     ["15", "15*23", "60*18"]])
def test_projection_repetitions_expand_during_config_loading(tmp_path, entries):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser"]["projection_resolution_mins"] = [*entries, "240*6"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).energy_plan.optimiser.projection_resolution_mins == (15,) * 24 + (60,) * 18 + (240,) * 6


def test_each_optimiser_resolution_must_cover_total_horizon(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser"]["projection_resolution_mins"] = ["60*47"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="must cover num_projection_hours"):
        load_config(path)


@pytest.mark.parametrize("visible", [0, -1, 49])
def test_visible_horizon_must_fit_inside_total_horizon(tmp_path, visible):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["num_visible_projection_hours"] = visible
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="num_visible_projection_hours"):
        load_config(path)


def test_protection_adjustment_is_required_and_accepts_values_above_100(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    del raw["energy_plan"]["protection_preference"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="energy_plan.protection_preference"):
        load_config(path)
    raw["energy_plan"]["protection_preference"] = 200.
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).energy_plan.protection_preference == 200.
