from __future__ import annotations
from solar_model.config import ScenarioMultiplierConfig
from solar_model.planning.types import ForecastData, ForecastInterval, ScenarioData

def create_scenarios(forecast_baseline: ForecastData, settings: ScenarioMultiplierConfig) -> list[ScenarioData]:
    """Scale the baseline into nine Markov condition inputs without timing stress."""
    scenarios = []
    for load_name, load_level in settings.house_load.items():
        for solar_name, solar_level in settings.solar.items():
            name = "med" if load_name == solar_name == "med" else f"{load_name}_{solar_name}"
            solar = [ForecastInterval(interval.starts_at, interval.ends_at, interval.kwh * solar_level) for interval in forecast_baseline.solar]
            load = [ForecastInterval(interval.starts_at, interval.ends_at, interval.kwh * load_level) for interval in forecast_baseline.load]
            scenarios.append(ScenarioData(name, ForecastData(solar, load)))
    return scenarios
