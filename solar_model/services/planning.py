"""Assemble the planning model's input snapshot from stored observations."""
from datetime import datetime

from solar_model.config import Config
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import SimulationResult
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.queries.planning import (
    load_battery_state_as_of,
    load_device_info_as_of,
    load_forecast_as_of,
)


def plan_as_of(database: SolarDatabase, config: Config, as_of: datetime) -> SimulationResult:
    """Select only data known by the cutoff, then call the database-free model."""
    device = load_device_info_as_of(database, config, as_of)
    state = load_battery_state_as_of(database, as_of, device)
    forecast = load_forecast_as_of(database, as_of, config)
    return simulate_energy_plan(device, state, create_scenarios(forecast), config, as_of)
