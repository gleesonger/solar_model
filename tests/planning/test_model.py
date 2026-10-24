"""Exercise the public planning API with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import OptimisationWeightsConfig, TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning.model import PlanningInputs, build_plan
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval


def test_in_memory_plan_preserves_scenario_energy_and_cost():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    baseline = ForecastData("baseline", [ForecastInterval(start, end, 0.)],
                            [ForecastInterval(start, end, 2.)])
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
                     inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.,
                     charge_efficiency=.9, discharge_efficiency=.9)
    config = load_config()
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    minimum = TariffRule(days, "00:00", "24:00", 0., "minimum")
    maximum = replace(minimum, rate=10., name="maximum")
    tariffs = TariffsConfig((TariffPeriod("2000-01-01",
        (replace(minimum, rate=.4, name="flat"),), (replace(minimum, rate=.1),)),))
    config = replace(config, tariffs=tariffs, energy_plan=replace(
        config.energy_plan, num_projection_hours=2, projection_resolution_mins=15,
        minimum_battery_kwh=(minimum,), maximum_battery_kwh=(maximum,),
        optimisation_weights=OptimisationWeightsConfig(1., 0.),
    ))

    plans = build_plan(PlanningInputs(device, DeviceState(0.), baseline, start), config)

    assert len(plans) == 9
    for name, plan in plans.items():
        multiplier = .5 if name.startswith("low_") else 1.5 if name.startswith("high_") else 1.
        assert len(plan) == 8
        assert plan.load_kwh.sum() == pytest.approx(2 * multiplier)
        assert plan.solar_kwh.sum() == 0.
        assert plan.grid_import_kwh.tolist() == pytest.approx(plan.load_kwh.tolist())
        assert plan.net_cost.sum() == pytest.approx((plan.load_kwh * plan.import_rate).sum())
        assert plan.battery_end_kwh.tolist() == pytest.approx([0.] * 8, abs=1e-7)
    assert baseline.sum(start, end).load_kwh == 2.
