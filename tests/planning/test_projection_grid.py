"""Configured full horizons, report boundaries and variable-duration DP physics."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import load_config
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.storage.queries.planning import _forecast_bounds


@pytest.mark.parametrize("intervals", [dp.projection_intervals])
@pytest.mark.parametrize("minute,second,expected_second_end", [
    (5, 0, (10, 30)),
    (45, 0, (11, 15)),
    (50, 0, (11, 15)),
    (55, 30, (11, 15)),
])
def test_first_action_is_15_minutes_and_second_aligns(intervals, minute, second, expected_second_end):
    start = datetime(2026, 10, 27, 10, minute, second, tzinfo=timezone.utc)
    periods = intervals(load_config(), start)
    assert periods[0] == (start, start + timedelta(minutes=15))
    assert periods[1][1] == start.replace(hour=expected_second_end[0], minute=expected_second_end[1], second=0)
    assert periods[-1][1] == start + timedelta(hours=48)
    assert any(end == start + timedelta(hours=24) for _, end in periods)
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))


@pytest.mark.parametrize("intervals,expected_hidden", [
    (dp.projection_intervals, [5] + [60] * 23 + [55]),
])
def test_alignment_preserves_visible_and_lookahead_horizons(intervals, expected_hidden):
    start = datetime(2026, 10, 27, 10, 55, tzinfo=timezone.utc)
    periods = intervals(load_config(), start)
    visible_end = start + timedelta(hours=24)
    hidden = [(a, b) for a, b in periods if a >= visible_end]
    assert [(b - a).total_seconds() / 60 for a, b in hidden] == expected_hidden
    assert hidden[0][0] == visible_end
    assert hidden[-1][1] == start + timedelta(hours=48)


@pytest.mark.parametrize("intervals", [dp.projection_intervals])
def test_first_action_is_15_minutes_even_with_longer_configured_step(intervals):
    config = load_config()
    optimiser = config.energy_plan.optimiser
    optimiser = replace(optimiser, projection_resolution_mins=(60,) * 48)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=optimiser))
    start = datetime(2026, 10, 27, 10, 50, tzinfo=timezone.utc)
    periods = intervals(config, start)
    assert periods[0] == (start, start + timedelta(minutes=15))
    assert periods[1][1] == datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    assert periods[-1][1] == start + timedelta(hours=48)


@pytest.mark.parametrize("intervals,visible_count,hidden_count", [(dp.projection_intervals, 97, 25)])
def test_configured_48_hours_includes_configured_lookahead_steps(intervals, visible_count, hidden_count):
    config = load_config()
    start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
    periods = intervals(config, start)
    visible_end = start + timedelta(hours=24)
    assert len(periods) == visible_count + hidden_count
    assert periods[visible_count - 1][1] == periods[visible_count][0] == visible_end
    assert periods[-1][1] == start + timedelta(hours=48)
    hidden_minutes = [(end - begin).total_seconds() / 60 for begin, end in periods[visible_count:]]
    assert hidden_minutes == ([55] + [60] * 23 + [5] if intervals is dp.projection_intervals else [235] + [240] * 5 + [5])
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))
    forecast_start, forecast_end = _forecast_bounds(start, config)
    assert forecast_start <= start and forecast_end == (start + timedelta(hours=49)).replace(minute=0)


@pytest.mark.parametrize("intervals", [dp.projection_intervals])
def test_nonstandard_total_and_visible_horizon_are_not_hard_coded(intervals):
    config = load_config()
    steps = (15,) * 48 + (120,) * 9
    settings = config.energy_plan.optimiser
    settings = replace(settings, projection_resolution_mins=steps)
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=30, num_visible_projection_hours=12, optimiser=settings))
    start = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    periods = intervals(config, start)
    hidden = [(begin, end) for begin, end in periods if begin >= start + timedelta(hours=12)]
    hidden_minutes = [(end - begin).total_seconds() / 60 for begin, end in hidden]
    assert hidden_minutes == [115] + [120] * 8 + [5]
    assert periods[-1][1] == start + timedelta(hours=30)


def test_dp_mixes_15_and_30_minute_visible_steps_then_four_hour_steps():
    config = load_config()
    settings = replace(config.energy_plan.optimiser, projection_resolution_mins=(15,) * 48 + (30,) * 24 + (240,) * 6)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=settings))
    start = datetime(2026, 3, 29, tzinfo=timezone.utc)
    periods = dp.projection_intervals(config, start)
    assert [(end - begin).total_seconds() / 60 for begin, end in periods] == [15] * 48 + [30] * 24 + [240] * 6
    assert periods[-1][1] == start + timedelta(hours=48)


@pytest.mark.parametrize("hours", [.25, .5, 4.])
@pytest.mark.parametrize("mode", [dp.OperatingMode.CHARGE_PV_FIRST, dp.OperatingMode.DISCHARGE_PV_FIRST])
def test_dp_scales_flows_and_costs_by_each_period_duration(hours, mode):
    device = dp.DeviceParameters(1., 1., .95, .9, 5., 5., 5., 5.)
    period = dp.Period(0., hours * 3600, 0., 0., .1, .2, "flat", 0., 18., 999., hours, 0., 3.)
    step = dp.simulate_system_single_time_period(device, 18., period, 8., dp.Action(mode, 1.))
    if mode == dp.OperatingMode.CHARGE_PV_FIRST:
        assert step.charge == pytest.approx(hours)
        assert step.battery_end == pytest.approx(8. + .95 * hours)
        assert step.net_cost == pytest.approx(.1 * hours)
    else:
        assert step.discharge == pytest.approx(hours)
        assert step.battery_end == pytest.approx(8. - hours / .9)
        assert step.net_cost == pytest.approx(-.2 * hours)


@pytest.mark.parametrize("minute,steps,hours,expected", [
    (5, (15,) * 8, 2, [15, 10, 15, 15, 15, 15, 15, 15, 5]),
    (45, (15,) * 8, 2, [15] * 8),
    (55, (15,) * 8, 2, [15, 5] + [15] * 6 + [10]),
    (0, (15,) * 8, 2, [15] * 8),
    (5, (40, 40, 40, 60), 3, [15, 40, 40, 20, 60, 5]),
    (5, (55, 30, 95), 3, [15, 10, 95, 60]),
    (5, (15,) * 12, 2, [15, 10, 15, 15, 15, 15, 15, 15, 5]),
])
def test_dp_keeps_first_15_minutes_and_aligns_later_steps(minute, steps, hours, expected):
    config = load_config()
    optimiser = config.energy_plan.optimiser
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=hours, num_visible_projection_hours=hours,
        optimiser=replace(optimiser, projection_resolution_mins=steps)))
    start = datetime(2026, 10, 27, 10, minute, tzinfo=timezone.utc)
    periods = dp.projection_intervals(config, start)
    assert [(end - begin).total_seconds() / 60 for begin, end in periods] == expected
    assert periods[0][0] == start
    assert periods[0][1] == start + timedelta(minutes=15)
    assert periods[-1][1] == start + timedelta(hours=hours)
    assert all(begin < end for begin, end in periods)
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))


def test_dp_aligns_fractional_seconds_without_losing_time():
    config = load_config()
    start = datetime(2026, 10, 24, 23, 55, 30, 123456, tzinfo=timezone.utc)
    periods = dp.projection_intervals(config, start)
    assert periods[0] == (start, start + timedelta(minutes=15))
    assert periods[1][1] == datetime(2026, 10, 25, 0, 15, tzinfo=timezone.utc)
    assert periods[-1][1] - periods[-1][0] == timedelta(minutes=55, seconds=30, microseconds=123456)
    assert sum((end - begin for begin, end in periods), timedelta()) == timedelta(hours=config.energy_plan.num_projection_hours)


def test_dp_does_not_fill_an_underconfigured_horizon_with_deferred_time():
    config = load_config()
    optimiser = config.energy_plan.optimiser
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=2, num_visible_projection_hours=2,
        optimiser=replace(optimiser, projection_resolution_mins=(15,) * 7)))
    with pytest.raises(ValueError, match="must cover num_projection_hours"):
        dp.projection_intervals(config, datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc))
