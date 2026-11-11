"""Synthetic hardware and forecast inputs shared by Markov and physics checks."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import pytest
from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, ScenarioData

@pytest.fixture
def inputs():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
        inverter_max_active_power_kw=4., inverter_max_absorption_power_kw=4.,
        inverter_battery_charge_power_kw=4., inverter_battery_discharge_power_kw=4.,
        battery_charge_efficiency=1., battery_discharge_efficiency=1., grid_max_import_power_kw=float("inf"))
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", 0., "flat")
    config = load_config()
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (replace(rule, rate=.4),), (replace(rule, rate=.1),)),)),
        energy_plan=replace(config.energy_plan, num_projection_hours=25, num_visible_projection_hours=1,
            optimiser=replace(config.energy_plan.optimiser, compile_model_to_native_code=False, projection_resolution_mins=(15,) * 4 + (240,) * 6),
            minimum_battery_kwh=(rule,), maximum_battery_kwh=(replace(rule, rate=10.),), limit_battery_export_rate_to_grid_kw=(), protection_preference=0.))
    scenarios = [ScenarioData("base", ForecastData([ForecastInterval(start, end, 0.), ForecastInterval(end, end + timedelta(hours=24), 0.)], [ForecastInterval(start, end, 1.), ForecastInterval(end, end + timedelta(hours=24), 0.)]))]
    return device, DeviceState(2.), scenarios, config, start
