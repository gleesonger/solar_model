"""Assemble the planning model's input snapshot from stored observations."""
from datetime import datetime

import pandas as pd

from solar_model.config import Config
from solar_model.planning.model import PlanningInputs, build_plan
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.queries.planning import (
    load_battery_state_as_of,
    load_device_info_as_of,
    load_forecast_as_of,
)


def plan_as_of(database: SolarDatabase, config: Config, as_of: datetime) -> dict[str, pd.DataFrame]:
    """Select only data known by the cutoff, then call the database-free model."""
    device = load_device_info_as_of(database, config, as_of)
    inputs = PlanningInputs(device, load_battery_state_as_of(database, as_of, device),
                            load_forecast_as_of(database, as_of, config), as_of)
    return build_plan(inputs, config)
