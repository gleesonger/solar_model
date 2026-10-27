from __future__ import annotations
from datetime import datetime, timedelta
from solar_model.planning.types import ForecastData, ForecastInterval, ScenarioData

# Scenario name -> (energy multiplier, optional first/second half-hour split).
LOAD_SCENARIOS = {
    'low': (0.5, None),
    'base': (1.0, None),
    'high': (1.5, (0.8, 0.2)),
}

SOLAR_SCENARIOS = {
    'low': (0.5, None),
    'base': (1.0, None),
    'high': (1.5, (0.25, 0.75)),
}

def create_scenarios(forecast_baseline: ForecastData) -> list[ScenarioData]:
    """Create load_solar scenarios with ±50% totals and high-case timing stress."""
    solar_half_hours = _half_hour_intervals(forecast_baseline.solar)
    load_half_hours = _half_hour_intervals(forecast_baseline.load)
    scenarios = []
    probability = 1 / (len(LOAD_SCENARIOS) * len(SOLAR_SCENARIOS))
    for load_name, (load_multiplier, load_split) in LOAD_SCENARIOS.items():
        for solar_name, (solar_multiplier,
        solar_split) in SOLAR_SCENARIOS.items():
            name = "base" if load_name == solar_name == "base" else f"{load_name}_{solar_name}"

            stressed_solar = _scenario_intervals(solar_half_hours, solar_multiplier, solar_split)
            stressed_load = _scenario_intervals(load_half_hours, load_multiplier, load_split)

            scenarios.append(ScenarioData(name, ForecastData(stressed_solar, stressed_load), probability))
    return scenarios

def _half_hour_intervals(intervals: tuple[ForecastInterval, ...]) -> list[tuple[datetime, float]]:
    """Sample one forecast series into contiguous half-hour energy intervals without extrapolation."""
    starts_at = intervals[0].starts_at
    ends_at = intervals[-1].ends_at
    if starts_at.minute % 30 or starts_at.second or starts_at.microsecond or (ends_at - starts_at).total_seconds() % (30 * 60):
        raise ValueError("forecast coverage must start and end on a 30-minute boundary")
    half_hours = []
    while starts_at < ends_at:
        ends_at_half_hour = starts_at + timedelta(minutes=30)
        half_hours.append((starts_at, ForecastData._sum_intervals(intervals, starts_at, ends_at_half_hour)))
        starts_at = ends_at_half_hour
    return half_hours

def _scenario_intervals(half_hours: list[tuple[datetime, float]], multiplier: float, stress_split: tuple[float, float] | None) -> list[ForecastInterval]:
    """Scale each half-hour, or redistribute each pair when a high-case stress split applies."""
    if len(half_hours) % 2:
        raise ValueError("forecast coverage must contain whole hours")
    intervals = []
    for (first_starts_at, first), (second_starts_at, second) in zip(half_hours[::2], half_hours[1::2]):
        first_kwh = first * multiplier
        second_kwh = second * multiplier
        if stress_split:
            total_kwh = first_kwh + second_kwh
            first_kwh, second_kwh = total_kwh * stress_split[0], total_kwh * stress_split[1]
        intervals.extend((ForecastInterval(first_starts_at, first_starts_at + timedelta(minutes=30), first_kwh), ForecastInterval(second_starts_at, second_starts_at + timedelta(minutes=30), second_kwh)))
    return intervals
