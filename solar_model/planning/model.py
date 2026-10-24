"""Pure planning entry point, callable with in-memory inputs and no database."""
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from solar_model.config import Config
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData


@dataclass(frozen=True)
class PlanningInputs:
    device_info: DeviceInfo
    device_state: DeviceState
    forecast: ForecastData
    starts_at: datetime


def build_plan(inputs: PlanningInputs, config: Config) -> dict[str, pd.DataFrame]:
    """Generate scenarios and return the current read-only simulation for each."""
    scenarios = create_scenarios(inputs.forecast)
    return simulate_energy_plan(inputs.device_info, inputs.device_state,
                                list(scenarios.values()), config, inputs.starts_at)
