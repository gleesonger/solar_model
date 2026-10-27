"""Exercise the public planning API with synthetic inputs and the real solver."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning.optimisation import _projection_intervals, simulate_energy_plan
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, ScenarioData


@pytest.mark.parametrize("resolution,expected_durations", [(15, [.25] * 8), (120, [2.])])
def test_in_memory_plan_preserves_scenario_energy_and_cost(resolution, expected_durations):
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
        config.energy_plan, num_projection_hours=2, projection_resolution_mins=resolution,
        minimum_battery_kwh=(minimum,), maximum_battery_kwh=(maximum,),
        protection_preference=0.,
    ))

    scenarios = create_scenarios(baseline)
    forecasts = {scenario.name: scenario.forecast_data for scenario in scenarios}
    plans = simulate_energy_plan(device, DeviceState(0.), scenarios, config, start).results

    assert len(plans) == 9
    for name, plan in plans.items():
        multiplier = .5 if name.startswith("low_") else 1.5 if name.startswith("high_") else 1.
        assert len(plan) == len(expected_durations)
        durations = [(b - a).total_seconds() / 3600 for a, b in zip(plan.starts_at, plan.ends_at)]
        assert durations == expected_durations
        assert plan.grid_import_kwh.tolist() == pytest.approx([forecasts[name].sum(a, b).load_kwh for a, b in zip(plan.starts_at, plan.ends_at)])
        assert plan.battery_start_kwh.tolist() == pytest.approx([0.] * len(plan))
        assert plan.load_kwh.sum() == pytest.approx(2 * multiplier)
        assert plan.solar_kwh.sum() == 0.
        assert plan.grid_import_kwh.tolist() == pytest.approx(plan.load_kwh.tolist(), abs=1e-5)
        assert plan.net_cost.sum() == pytest.approx((plan.load_kwh * plan.import_rate).sum(), abs=1e-5)
        assert plan.battery_end_kwh.tolist() == pytest.approx([0.] * len(plan), abs=1e-5)
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


def test_each_call_builds_a_fresh_model_with_current_inputs(monkeypatch, caplog):
    import cvxpy as cp
    from solar_model.planning import optimisation

    caplog.set_level("INFO", logger=optimisation.__name__)
    start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
    config = load_config()
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", .4, "flat")
    weekday_rule = replace(rule, days=("Tue",))
    other_days = replace(rule, days=("Mon", "Wed", "Thu", "Fri", "Sat", "Sun"), rate=.6)
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (weekday_rule, other_days), (replace(rule, rate=0.),)),)),
                     energy_plan=replace(config.energy_plan, num_projection_hours=1, protection_preference=0.,
                                         minimum_battery_kwh=(replace(rule, rate=0.),),
                                         maximum_battery_kwh=(replace(rule, rate=10.),)))
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
                     inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.,
                     charge_efficiency=.95, discharge_efficiency=.95)
    problems = []
    original_solve = cp.Problem.solve

    def solve(problem, *args, **kwargs):
        problems.append(problem)
        return original_solve(problem, *args, **kwargs)

    monkeypatch.setattr(cp.Problem, "solve", solve)

    def run(when, battery, demand, cfg=config, info=device, probability=1.):
        end = when + timedelta(hours=1)
        forecast = ForecastData([ForecastInterval(when, end, 0.)], [ForecastInterval(when, end, demand)])
        return simulate_energy_plan(info, DeviceState(battery), [ScenarioData("base", forecast, probability)], cfg, when).results["base"]

    first = run(start, 0., 1.)
    second = run(start + timedelta(days=1), 1., 2.)
    assert problems[0] is not problems[1]
    assert first.battery_start_kwh.iloc[0] == 0.
    assert first.load_kwh.sum() == pytest.approx(1.)
    assert first.import_rate.iloc[0] == .4
    assert second.import_rate.iloc[0] == .6
    assert second.battery_start_kwh.iloc[0] == 1.
    assert second.load_kwh.sum() == pytest.approx(2.)
    assert second.starts_at.iloc[0] == start + timedelta(days=1)
    assert "plan_model complexity:" in caplog.text
    assert "time_steps=[15*1, 10*1, 15*2, 5*1]" in caplog.text
    assert "plan_model end: status=success" in caplog.text


def test_discharge_power_limit_scales_with_each_mixed_interval():
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    forecast = ForecastData([ForecastInterval(start, end, 0.)], [ForecastInterval(start, end, 6.)])
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
                     inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=2.,
                     charge_efficiency=.9, discharge_efficiency=.9)
    config = load_config()
    flat = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", .4, "flat")
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (flat,), (replace(flat, rate=0.),)),)),
        energy_plan=replace(config.energy_plan, num_projection_hours=2, projection_resolution_mins=15,
                            minimum_battery_kwh=(replace(flat, rate=0.),), maximum_battery_kwh=(replace(flat, rate=10.),),
                            limit_battery_export_rate_to_grid_kw=(replace(flat, rate=0.),), protection_preference=0.))
    frame = simulate_energy_plan(device, DeviceState(10.), [ScenarioData("base", forecast, 1.)], config, start).results["base"]
    assert frame.battery_to_load_kwh.tolist() == pytest.approx([.5] * 8)
    assert frame.grid_import_kwh.tolist() == pytest.approx([.25] * 8)
    assert frame.battery_end_kwh.tolist() == pytest.approx([10 - energy / .9 for energy in [.5 * index for index in range(1, 9)]])


def test_mixed_projection_grid_ignores_tariff_and_battery_schedule_boundaries():
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
    assert durations == [.25] * 24 + [1.] * 18
    assert sum(durations) == 24
    assert periods[0][0] == start and periods[-1][1] == start + timedelta(hours=24)
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))
    cheap = replace(flat, end_time="00:20", rate=.1)
    peak = replace(flat, start_time="00:20", rate=.4)
    config = replace(config, tariffs=TariffsConfig((TariffPeriod("2000-01-01", (cheap, peak), (flat,)),)))
    config = replace(config, energy_plan=replace(config.energy_plan, minimum_battery_kwh=(cheap, peak)))
    assert _projection_intervals(config, start) == periods


@pytest.mark.parametrize("minute,expected", [
    (0, [15] * 24 + [60] * 18),
    (5, [15, 10] + [15] * 22 + [60] * 18 + [5]),
    (15, [15] * 23 + [60] * 18 + [15]),
    (50, [15, 10] + [15] * 19 + [60] * 18 + [50]),
])
def test_projection_keeps_first_action_and_aligns_following_periods(minute, expected):
    config = replace(load_config(), timezone="UTC")
    start = datetime(2026, 10, 27, 10, minute, tzinfo=timezone.utc)
    periods = _projection_intervals(config, start)
    assert [(b - a).total_seconds() / 60 for a, b in periods] == expected
    assert periods[0] == (start, start + timedelta(minutes=15))
    assert periods[-1][1] == start + timedelta(hours=24)
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))


@pytest.mark.parametrize("zone,start", [
    ("Europe/Dublin", datetime(2026, 3, 29, 0, 55, tzinfo=timezone.utc)),
    ("Europe/Dublin", datetime(2026, 10, 25, 0, 55, tzinfo=timezone.utc)),
    ("Asia/Kathmandu", datetime(2026, 10, 27, 4, 20, 7, tzinfo=timezone.utc)),
])
def test_clock_aligned_projection_preserves_elapsed_time_across_timezones(zone, start):
    config = replace(load_config(), timezone=zone)
    periods = _projection_intervals(config, start)
    utc_periods = [(a.astimezone(timezone.utc), b.astimezone(timezone.utc)) for a, b in periods]
    assert utc_periods[0] == (start, start + timedelta(minutes=15))
    projection_end = start + timedelta(hours=24)
    assert utc_periods[-1][1] == projection_end
    assert sum((b - a).total_seconds() for a, b in utc_periods) == (projection_end - start).total_seconds()
    assert all(a < b for a, b in utc_periods)
    assert all(left[1] == right[0] for left, right in zip(utc_periods, utc_periods[1:]))
    assert all(b.minute % 15 == b.second == b.microsecond == 0 for _, b in periods[1:-1])
