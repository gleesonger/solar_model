"""Every declared file/editor setting, including nullable values, is mandatory."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import os
import subprocess
import sys

import pytest
import yaml

from solar_model.config import config_from_mapping, energy_plan_yaml, load_config, parse_energy_plan_yaml


COMPLETE = yaml.safe_load(yaml.safe_dump(asdict(load_config())))


def value_paths(value, prefix=()):
    if prefix == ("dashboard", "actuals_to_forecast"):
        return  # Mapping entries are user-selected PV strings, not declared fields.
    if isinstance(value, dict):
        for key, child in value.items():
            yield (*prefix, key)
            yield from value_paths(child, (*prefix, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from value_paths(child, (*prefix, index))


@pytest.mark.parametrize("path", list(value_paths(COMPLETE)), ids=lambda path: ".".join(map(str, path)))
def test_every_declared_configuration_value_is_required(path):
    raw = deepcopy(COMPLETE)
    parent = raw
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(ValueError, match="missing required configuration value"):
        config_from_mapping(raw)


def test_explicit_nulls_and_complete_editor_round_trip():
    config = load_config()
    assert config_from_mapping(deepcopy(COMPLETE)) == config
    text = energy_plan_yaml(config.energy_plan)
    assert "name: null" in text
    assert parse_energy_plan_yaml(config, text) == config.energy_plan
    raw = yaml.safe_load(text)
    del raw["history"]["retain_snapshots_period_days"]
    with pytest.raises(ValueError, match="energy_plan.history.retain_snapshots_period_days"):
        parse_energy_plan_yaml(config, yaml.safe_dump(raw))


def test_incomplete_startup_crashes_before_database_creation(tmp_path):
    raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    del raw["energy_plan"]["history"]["retain_snapshots_frequency_mins"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    root = str(Path(__file__).resolve().parents[2])
    environment = os.environ | {"PYTHONPATH": root}
    result = subprocess.run([sys.executable, "-m", "solar_model.runtime.supervisor"], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "energy_plan.history.retain_snapshots_frequency_mins" in result.stderr
    assert not (tmp_path / "solar.db").exists()
