"""Assemble the planning model's input snapshot from stored observations."""
from datetime import datetime
from dataclasses import dataclass, replace
import math
import os
from zoneinfo import ZoneInfo
import pandas as pd

from solar_model.config import Config
from solar_model.logging_setup import configure_logging
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import SimulationResult
from solar_model.storage.connection import SolarDatabase, create_engine_for_database
from solar_model.storage.queries.planning import (
    load_battery_state_as_of,
    load_device_info_as_of,
    load_forecast_as_of,
    load_plan_actuals,
)


def warm_planning_worker() -> int:
    """Import planning dependencies and initialise HiGHS without loading or solving a plan."""
    import highspy
    highspy.Highs()
    return os.getpid()


def plan_as_of(database: SolarDatabase, config: Config, as_of: datetime) -> SimulationResult:
    """Select only data known by the cutoff, then call the database-free model."""
    device = load_device_info_as_of(database, config, as_of)
    state = load_battery_state_as_of(database, as_of, device)
    forecast = load_forecast_as_of(database, as_of, config)
    return simulate_energy_plan(device, state, create_scenarios(forecast), config, as_of)


@dataclass
class PlanReport:
    simulation: SimulationResult
    actuals: pd.DataFrame
    battery_capacity_kwh: float | None = None


def plan_with_preference(config: Config, as_of: datetime, preference: float) -> PlanReport:
    """Generate a report with a request-local preference, without migrating storage."""
    if not math.isfinite(preference) or preference < 0:
        raise ValueError("Protection adjustment must be finite and non-negative")
    configure_logging(config.logging.level)
    config = replace(config, energy_plan=replace(config.energy_plan, protection_preference=preference))
    database = SolarDatabase(create_engine_for_database(config.database.path))
    try:
        result = plan_as_of(database, config, as_of)
        capacity = load_device_info_as_of(database, config, as_of).system_battery_capacity_kwh
        day_start = as_of.astimezone(ZoneInfo(config.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        return PlanReport(result, load_plan_actuals(database, day_start, as_of, capacity), capacity)
    finally:
        database.close()
