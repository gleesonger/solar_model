"""Assemble the planning model's input snapshot from stored observations."""
from datetime import datetime, timezone
from dataclasses import dataclass, replace
import os
from zoneinfo import ZoneInfo
import pandas as pd

from solar_model.config import Config, EnergyPlanConfig, ScenariosConfig, validate_config
from solar_model.logging_setup import configure_logging
from solar_model.planning.plan_optimisation_milp import simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import SimulationResult
from solar_model.storage.connection import SolarDatabase, create_engine_for_database
from solar_model.storage.queries.planning import (
    load_battery_state_as_of,
    load_device_info_as_of,
    load_forecast_as_of,
    load_plan_actuals,
    load_latest_full_update,
)


def warm_planning_worker() -> int:
    """Import planning dependencies and initialise HiGHS without loading or solving a plan."""
    import highspy
    highspy.Highs()
    return os.getpid()


def plan_as_of(database: SolarDatabase, config: Config, as_of: datetime, *, battery_as_of: datetime | None = None) -> SimulationResult:
    """Select only data known by the cutoff, then call the database-free model."""
    model = config.energy_plan.optimiser.model
    if model == "mix_integer_linear_prog":
        optimiser = simulate_energy_plan
    elif model == "dynamic_programming":
        from solar_model.planning.plan_optimisation_dynamic_programming import simulate_energy_plan as optimiser
    else:
        raise ValueError("energy_plan.optimiser.model must be mix_integer_linear_prog or dynamic_programming")
    device = load_device_info_as_of(database, config, as_of)
    state = load_battery_state_as_of(database, battery_as_of or as_of, device)
    forecast = load_forecast_as_of(database, as_of, config)
    return optimiser(device, state, create_scenarios(forecast, config.energy_plan.scenarios), config, as_of)


@dataclass
class PlanReport:
    simulation: SimulationResult
    actuals: pd.DataFrame
    battery_capacity_kwh: float | None = None
    scenarios: ScenariosConfig | None = None
    preferred_reserve_kwh: float | None = None
    protection_preference: float | None = None
    command_battery_capacity_kwh: float | None = None
    full_data_at: datetime | None = None
    details: dict[str, dict[str, pd.DataFrame]] | None = None
    summaries: dict[str, dict[str, float]] | None = None
    action_minutes: float | None = None
    action_solar_export: dict[str, float] | None = None
    full_resolution: bool = True


def latest_full_update(database_path: str) -> datetime | None:
    """Read the marker with a short-lived session, retaining the cached connection pool."""
    database = SolarDatabase(create_engine_for_database(database_path))
    return load_latest_full_update(database, datetime.now(timezone.utc))


def generate_plan_report(config: Config, as_of: datetime, energy_plan_override: EnergyPlanConfig | None = None) -> PlanReport:
    """Generate a UI report using an optional request-local energy_plan overlay."""
    if energy_plan_override is not None:
        config = replace(config, energy_plan=energy_plan_override)
    validate_config(config)
    configure_logging(config.logging.level)
    database = SolarDatabase(create_engine_for_database(config.database.path))
    try:
        full_data_at = load_latest_full_update(database, as_of)
        result = plan_as_of(database, config, as_of, battery_as_of=full_data_at)
        device = load_device_info_as_of(database, config, as_of)
        capacity = device.system_battery_capacity_kwh
        command_capacity = capacity * device.battery_state_of_health_percent / 100
        day_start = as_of.astimezone(ZoneInfo(config.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        return PlanReport(
            simulation=result,
            actuals=load_plan_actuals(database, day_start, as_of, capacity),
            battery_capacity_kwh=capacity,
            scenarios=config.energy_plan.scenarios,
            preferred_reserve_kwh=config.energy_plan.preferred_reserve_kwh,
            protection_preference=config.energy_plan.protection_preference,
            command_battery_capacity_kwh=command_capacity,
            full_data_at=full_data_at,
        )
    finally:
        database.close()
