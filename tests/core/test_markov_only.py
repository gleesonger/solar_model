"""The application has one explicitly configured Markov planning implementation."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from solar_model.config import energy_plan_yaml, load_config, parse_energy_plan_yaml


def test_editor_has_only_required_markov_settings() -> None:
    config = load_config()
    settings = yaml.safe_load(energy_plan_yaml(config.energy_plan))["optimiser"]
    assert set(settings) == {"compile_model_to_native_code", "transition_hours", "transition_matrix", "projection_resolution_mins", "grid_step_percent", "power_divisions"}
    assert parse_energy_plan_yaml(config, energy_plan_yaml(config.energy_plan)) == config.energy_plan


@pytest.mark.parametrize("key", ["model", "uncertainty_model", "mix_integer_linear_prog", "dynamic_programming", "refinement_stages", "use_numba"])
def test_removed_model_settings_are_rejected(key: str) -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["optimiser"][key] = "retired"
    with pytest.raises(ValueError, match="Markov-only"):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


def test_planner_and_reports_import_without_retired_solver_dependencies() -> None:
    code = """
import importlib.abc
import sys
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('cvxpy', 'highspy') or fullname.endswith('plan_optimisation_milp'):
            raise AssertionError('Retired solver imported: ' + fullname)
sys.meta_path.insert(0, BlockRetired())
from solar_model.services.planning import plan_as_of
from solar_model.planning.plan_report import scenario_summary
from solar_model.dashboard.tabs.plan import PlanTab
assert 'numba' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("enabled", [False, True])
def test_native_compilation_setting_is_required_and_round_trips(enabled: bool) -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["optimiser"]["compile_model_to_native_code"] = enabled
    parsed = parse_energy_plan_yaml(config, yaml.safe_dump(data))
    assert parsed.optimiser.compile_model_to_native_code is enabled
    assert parse_energy_plan_yaml(config, energy_plan_yaml(parsed)) == parsed
    del data["optimiser"]["compile_model_to_native_code"]
    with pytest.raises(ValueError, match="missing required configuration value: energy_plan.optimiser.compile_model_to_native_code"):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


@pytest.mark.parametrize("dimension", ["solar", "house_load"])
def test_each_transition_matrix_is_required_and_validated_separately(dimension: str) -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    del data["optimiser"]["transition_matrix"][dimension]
    with pytest.raises(ValueError, match="missing required configuration value: energy_plan.optimiser.transition_matrix." + dimension):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["optimiser"]["transition_matrix"][dimension][0] = [.6, .3, .2]
    with pytest.raises(ValueError, match="optimiser.transition_matrix." + dimension):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))


def test_old_shared_transition_matrix_is_not_silently_reused() -> None:
    config = load_config()
    data = yaml.safe_load(energy_plan_yaml(config.energy_plan))
    data["optimiser"]["transition_matrix"] = data["optimiser"]["transition_matrix"]["solar"]
    with pytest.raises(ValueError, match="transition_matrix must be a mapping"):
        parse_energy_plan_yaml(config, yaml.safe_dump(data))
