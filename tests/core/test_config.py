from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
import yaml

from solar_model.config import load_config


def test_optimiser_specific_sections_load_independently(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    settings = raw["energy_plan"]["optimiser_specific"]
    settings["mix_integer_linear_prog"]["projection_resolution_mins"] = ["60*48"]
    settings["dynamic_programing"]["projection_resolution_mins"] = ["10*288"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    loaded = load_config(path).energy_plan.optimiser_specific
    assert loaded.mix_integer_linear_prog.projection_resolution_mins == (60,) * 48
    assert loaded.dynamic_programing.projection_resolution_mins == (10,) * 288


@pytest.mark.parametrize("name,value", [("projection_resolution_mins", [15] * 96), ("dynamic_programming", {})])
def test_legacy_flat_optimiser_settings_require_explicit_migration(tmp_path, name, value):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"][name] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="move optimiser settings under energy_plan.optimiser_specific"):
        load_config(path)


def test_dp_defaults_and_custom_refinement_settings_load(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser_specific"].pop("dynamic_programing")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_config(path).energy_plan.optimiser_specific.dynamic_programing
    assert settings.use_numba is True
    assert settings.projection_resolution_mins == (15,) * 96 + (60,) * 24
    assert [(s.grid_step_percent, s.power_divisions, s.band_percent) for s in settings.refinement_stages] == [(2., 5, None), (1., 10, 5.), (1., 20, 3.), (1., 40, 1.)]
    raw["energy_plan"]["optimiser_specific"]["dynamic_programing"] = {"use_numba": False, "projection_resolution_mins": ["30*96"], "refinement_stages": [{"grid_step_percent": 4., "power_divisions": 3, "band_percent": None}, {"grid_step_percent": .5, "power_divisions": 20, "band_percent": 2.}]}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_config(path).energy_plan.optimiser_specific.dynamic_programing
    assert settings.use_numba is False
    assert settings.projection_resolution_mins == (30,) * 96
    assert settings.refinement_stages[0].grid_step_percent == 4.
    assert settings.refinement_stages[0].power_divisions == 3
    assert settings.refinement_stages[0].band_percent is None
    assert settings.refinement_stages[1].grid_step_percent == .5


@pytest.mark.parametrize("changes", [
    {"backend": "numba"},
    {"use_numba": "true"},
    {"use_numba": 1},
    {"use_numba": None},
    {"projection_resolution_mins": 0},
    {"coarse_grid_step_percent": float("nan")},
    {"coarse_power_divisions": 0},
    {"refinement_stages": []},
    {"refinement_stages": [{"grid_step_percent": 1., "power_divisions": 0, "band_percent": None}]},
    {"refinement_stages": [{"grid_step_percent": 0., "power_divisions": 5, "band_percent": None}]},
    {"refinement_stages": [{"grid_step_percent": 2., "power_divisions": 5, "band_percent": None}, {"grid_step_percent": 1., "power_divisions": 10, "band_percent": None}]},
    {"refinement_stages": [{"grid_step_percent": 1., "power_divisions": 10, "band_percent": 101.}]},
])
def test_invalid_dp_settings_are_rejected(tmp_path, changes):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser_specific"]["dynamic_programing"].update(changes)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="dynamic_programing"):
        load_config(path)


def test_forced_command_minimums_default_and_load_separately(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for name in ("min_forced_charge_power_kw", "min_forced_discharge_power_kw", "max_forced_charge_power_kw", "max_forced_discharge_power_kw"):
        raw["energy_plan"].pop(name, None)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_config(path).energy_plan
    assert settings.min_forced_charge_power_kw == .1
    assert settings.min_forced_discharge_power_kw == .1
    assert settings.max_forced_charge_power_kw == 999.
    assert settings.max_forced_discharge_power_kw == 999.
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


def test_scenario_levels_load_multiplier_and_probability(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["scenarios"]["solar"]["low"] = {"multiplier": .4, "probability": .1}
    raw["energy_plan"]["scenarios"]["solar"]["high"]["probability"] = .3
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path)
    assert config.energy_plan.scenarios.solar.low.multiplier == .4
    assert config.energy_plan.scenarios.solar.low.probability == .1
    assert config.energy_plan.scenarios.house_load.med.probability == .6


@pytest.mark.parametrize("dimension", ["solar", "house_load"])
@pytest.mark.parametrize("total", [.97, .98, 1., 1.02, 1.03])
def test_scenario_probabilities_within_three_percent_are_normalised(tmp_path, dimension, total):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for entry in raw["energy_plan"]["scenarios"][dimension].values():
        entry["probability"] *= total
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    loaded = getattr(load_config(path).energy_plan.scenarios, dimension)
    assert [level.probability for _, level in loaded.items()] == pytest.approx([.2, .6, .2])
    assert sum(level.probability for _, level in loaded.items()) == pytest.approx(1., abs=1e-12)


@pytest.mark.parametrize("total", [.969999, 1.030001, 0.])
def test_scenario_probability_totals_outside_tolerance_are_rejected(tmp_path, total):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for entry in raw["energy_plan"]["scenarios"]["solar"].values():
        entry["probability"] *= total
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="within 3%"):
        load_config(path)


@pytest.mark.parametrize("dimension", ["solar", "house_load"])
@pytest.mark.parametrize("field,value", [("multiplier", -.5), ("multiplier", float("nan")),
    ("multiplier", float("inf")), ("multiplier", False), ("multiplier", 2.),
    ("probability", -.1), ("probability", float("nan")), ("probability", float("inf")),
    ("probability", True), ("probability", 1.1), ("probability", .3)])
def test_scenario_levels_reject_invalid_values_and_probability_sums(tmp_path, dimension, field, value):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["scenarios"][dimension]["low"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario"):
        load_config(path)


@pytest.mark.parametrize("levels", [[.5, 1., 1.5], "0.5, 1, 1.5", {},
    {"low": {}, "med": {}}, {"low": {}, "med": {}, "high": {}, "extra": {}}])
def test_scenarios_require_exact_named_levels(tmp_path, levels):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["scenarios"]["solar"] = levels
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="scenarios"):
        load_config(path)


@pytest.mark.parametrize("entry", [{"multiplier": 1.}, {"probability": .6},
                                  {"multiplier": 1., "probability": .6, "extra": 1}])
def test_scenario_entries_require_exact_multiplier_and_probability(tmp_path, entry):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["scenarios"]["solar"]["med"] = entry
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="scenarios"):
        load_config(path)


def test_scenario_settings_are_required(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    del raw["energy_plan"]["scenarios"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="scenarios"):
        load_config(path)


def test_top_level_scenarios_require_moving_under_energy_plan(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["scenarios"] = raw["energy_plan"].pop("scenarios")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="move scenarios under energy_plan.scenarios"):
        load_config(path)


def test_load_config_parses_minimum_battery_backup_rules_and_ignores_comment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
timezone: Europe/Dublin
logging: {level: INFO}
database: {path: solar.db}
forecast:
  solar:
    interval_seconds: 3600
    timeout_seconds: 10
    endpoint: https://api.forecast.solar/estimate
    arrays: [{panel_id: 1, name: East, latitude: 0, longitude: 0, declination: 0, azimuth: 0, peak_kw: 1}]
dashboard: {host: 127.0.0.1, port: 8090, actuals_to_forecast: {pv1: East}}
additional_device_info: {charge_efficiency: 0.95, discharge_efficiency: 0.95}
energy_plan:
  scenarios:
    solar:
      low: {multiplier: 0.5, probability: 0.2}
      med: {multiplier: 1, probability: 0.6}
      high: {multiplier: 1.5, probability: 0.2}
    house_load:
      low: {multiplier: 0.5, probability: 0.2}
      med: {multiplier: 1, probability: 0.6}
      high: {multiplier: 1.5, probability: 0.2}
  num_projection_hours: 24
  optimiser_specific:
    mix_integer_linear_prog:
      projection_resolution_mins: [15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15]
  protection_preference: 10
  limit_battery_export_rate_to_grid_kw:
    - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
      start_time: "00:00"
      end_time: "24:00"
      rate: 5.5
  minimum_battery_kwh:
    comment: Retain this much energy for an outage.
    rules:
      - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
        start_time: "00:00"
        end_time: "24:00"
        rate: 4.5
  maximum_battery_kwh:
    - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
      start_time: "00:00"
      end_time: "24:00"
      rate: 18
tariffs:
  periods:
    - effective_from: "2000-01-01"
      import:
        - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
          name: day
          start_time: "00:00"
          end_time: "24:00"
          rate: 0.4
      export:
        - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
          start_time: "00:00"
          end_time: "24:00"
          rate: 0.2
actuals:
  data_retrival_schedule: {live_update_interval_seconds: 1, full_updated_interval_seconds: 60}
  modbus: {host: localhost, port: 502, timeout_seconds: 5, default_device_id: 1}
""".strip(),
        encoding="utf-8",
    )

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


@pytest.mark.parametrize("optimiser", ["mix_integer_linear_prog", "dynamic_programing"])
@pytest.mark.parametrize("entries", [["15*24", "60*18"], [15, "15 * 23", "60*18"],
                                     ["15", "15*23", "60*18"]])
def test_projection_repetitions_expand_during_config_loading(tmp_path, entries, optimiser):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser_specific"][optimiser]["projection_resolution_mins"] = [*entries, "240*6"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert getattr(load_config(path).energy_plan.optimiser_specific, optimiser).projection_resolution_mins == (15,) * 24 + (60,) * 18 + (240,) * 6


@pytest.mark.parametrize("optimiser", ["mix_integer_linear_prog", "dynamic_programing"])
def test_each_optimiser_resolution_must_cover_total_horizon(tmp_path, optimiser):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser_specific"][optimiser]["projection_resolution_mins"] = ["60*47"]
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


@pytest.mark.parametrize("entry", ["15*", "15*  ", "15*0", "15*-2", "0*24", "15*2*3", "15+24"])
def test_invalid_projection_repetitions_are_rejected(tmp_path, entry):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["optimiser_specific"]["mix_integer_linear_prog"]["projection_resolution_mins"] = [entry, "60*24"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid configuration"):
        load_config(path)


def test_protection_adjustment_defaults_to_50_and_accepts_values_above_100(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    del raw["energy_plan"]["protection_preference"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).energy_plan.protection_preference == 50.
    raw["energy_plan"]["protection_preference"] = 200.
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).energy_plan.protection_preference == 200.
