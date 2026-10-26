"""Exercise the public planning API with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning.optimisation import _projection_intervals, simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval


def test_in_memory_plan_preserves_scenario_energy_and_cost():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    baseline = ForecastData([ForecastInterval(start, end, 0.)],
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
        protection_preference=0.,
    ))

    scenarios = create_scenarios(baseline)
    forecasts = {scenario.name: scenario.forecast_data for scenario in scenarios}
    plans = simulate_energy_plan(device, DeviceState(0.), scenarios, config, start).results

    assert len(plans) == 9
    for name, plan in plans.items():
        multiplier = .5 if name.startswith("low_") else 1.5 if name.startswith("high_") else 1.
        assert len(plan) == 3
        durations = [(b - a).total_seconds() / 3600 for a, b in zip(plan.starts_at, plan.ends_at)]
        assert durations == [.25, 1., .75]
        assert plan.grid_import_kwh.tolist() == pytest.approx([forecasts[name].sum(a, b).load_kwh for a, b in zip(plan.starts_at, plan.ends_at)])
        assert plan.battery_start_kwh.tolist() == pytest.approx([0.] * 3)
        assert plan.load_kwh.sum() == pytest.approx(2 * multiplier)
        assert plan.solar_kwh.sum() == 0.
        assert plan.grid_import_kwh.tolist() == pytest.approx(plan.load_kwh.tolist(), abs=1e-5)
        assert plan.net_cost.sum() == pytest.approx((plan.load_kwh * plan.import_rate).sum(), abs=1e-5)
        assert plan.battery_end_kwh.tolist() == pytest.approx([0.] * 3, abs=1e-5)
    assert baseline.sum(start, end).load_kwh == 2.


def test_generated_scenarios_separate_names_and_probabilities_from_forecasts():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    forecast = ForecastData([ForecastInterval(start, end, 1.)], [ForecastInterval(start, end, 2.)])
    scenarios = create_scenarios(forecast)
    assert isinstance(scenarios, list)
    assert len({scenario.name for scenario in scenarios}) == 9
    assert sum(scenario.probability for scenario in scenarios) == pytest.approx(1.)
    assert all(scenario.probability == pytest.approx(1 / 9) for scenario in scenarios)
    base = next(scenario for scenario in scenarios if scenario.name == "base")
    assert base.forecast_data.sum(start, end) == forecast.sum(start, end)
    assert not hasattr(forecast, "name")


def test_mixed_projection_grid_preserves_horizon_and_tariff_boundaries():
    config = load_config()
    start = datetime(2026, 10, 26, tzinfo=timezone.utc)
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    flat = TariffRule(days, "00:00", "24:00", .3, "flat")
    settings = replace(config.energy_plan, num_projection_hours=24, projection_resolution_mins=15,
                       minimum_battery_kwh=(replace(flat, rate=0.),),
                       maximum_battery_kwh=(replace(flat, rate=18.),), limit_battery_export_rate_to_grid_kw=())
    config = replace(config, timezone="UTC", energy_plan=settings,
                     tariffs=TariffsConfig((TariffPeriod("2000-01-01", (flat,), (flat,)),)))
    periods = _projection_intervals(config, start)
    durations = [(b - a).total_seconds() / 3600 for a, b in periods]
    assert durations == [.25, *([1.] * 23), .75]
    assert periods[0][0] == start and periods[-1][1] == start + timedelta(hours=24)
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))
    cheap = replace(flat, end_time="00:30", rate=.1)
    peak = replace(flat, start_time="00:30", rate=.4)
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (cheap, peak), (flat,)),)))
    periods = _projection_intervals(config, start)
    boundary = start + timedelta(minutes=30)
    assert any(end == boundary for _, end in periods)
    assert all(not left < boundary < right for left, right in periods)
