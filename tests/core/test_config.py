from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
import yaml

from solar_model.config import load_config


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
  num_projection_hours: 24
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


@pytest.mark.parametrize("entries", [["15*24", "60*18"], [15, "15 * 23", "60*18"],
                                     ["15", "15*23", "60*18"]])
def test_projection_repetitions_expand_during_config_loading(tmp_path, entries):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["projection_resolution_mins"] = entries
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).energy_plan.projection_resolution_mins == (15,) * 24 + (60,) * 18


@pytest.mark.parametrize("entry", ["15*", "15*  ", "15*0", "15*-2", "0*24", "15*2*3", "15+24"])
def test_invalid_projection_repetitions_are_rejected(tmp_path, entry):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    raw["energy_plan"]["projection_resolution_mins"] = [entry, "60*24"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid configuration"):
        load_config(path)
