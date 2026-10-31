"""Configured full horizons, report boundaries and variable-duration DP physics."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import load_config
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.plan_optimisation_milp import _projection_intervals
from solar_model.storage.queries.planning import _forecast_bounds


@pytest.mark.parametrize("intervals,visible_count,hidden_count,hidden_hours", [(dp.projection_intervals, 96, 24, 1), (_projection_intervals, 28, 6, 4)])
def test_configured_48_hours_includes_configured_lookahead_steps(intervals, visible_count, hidden_count, hidden_hours):
    config = load_config()
    start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
    periods = intervals(config, start)
    visible_end = start + timedelta(hours=24)
    assert len(periods) == visible_count + hidden_count
    assert periods[visible_count - 1][1] == periods[visible_count][0] == visible_end
    assert periods[-1][1] == start + timedelta(hours=48)
    assert all(end - begin == timedelta(hours=hidden_hours) for begin, end in periods[visible_count:])
    assert all(left[1] == right[0] for left, right in zip(periods, periods[1:]))
    forecast_start, forecast_end = _forecast_bounds(start, config)
    assert forecast_start <= start and forecast_end == (start + timedelta(hours=49)).replace(minute=0)


@pytest.mark.parametrize("intervals", [dp.projection_intervals, _projection_intervals])
def test_nonstandard_total_and_visible_horizon_are_not_hard_coded(intervals):
    config = load_config()
    steps = (15,) * 48 + (120,) * 9
    settings = config.energy_plan.optimiser_specific
    settings = replace(settings, dynamic_programing=replace(settings.dynamic_programing, projection_resolution_mins=steps), mix_integer_linear_prog=replace(settings.mix_integer_linear_prog, projection_resolution_mins=steps))
    config = replace(config, energy_plan=replace(config.energy_plan, num_projection_hours=30, num_visible_projection_hours=12, optimiser_specific=settings))
    start = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    periods = intervals(config, start)
    hidden = [(begin, end) for begin, end in periods if begin >= start + timedelta(hours=12)]
    assert len(hidden) == 9
    assert all(end - begin == timedelta(hours=2) for begin, end in hidden)
    assert periods[-1][1] == start + timedelta(hours=30)


def test_dp_mixes_15_and_30_minute_visible_steps_then_four_hour_steps():
    config = load_config()
    settings = replace(config.energy_plan.optimiser_specific.dynamic_programing, projection_resolution_mins=(15,) * 48 + (30,) * 24 + (240,) * 6)
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser_specific=replace(config.energy_plan.optimiser_specific, dynamic_programing=settings)))
    start = datetime(2026, 3, 29, tzinfo=timezone.utc)
    periods = dp.projection_intervals(config, start)
    assert [(end - begin).total_seconds() / 60 for begin, end in periods] == [15] * 48 + [30] * 24 + [240] * 6
    assert periods[-1][1] == start + timedelta(hours=48)


@pytest.mark.parametrize("hours", [.25, .5, 4.])
@pytest.mark.parametrize("mode", [dp.OperatingMode.CHARGE_PV_FIRST, dp.OperatingMode.DISCHARGE_PV_FIRST])
def test_dp_scales_flows_and_costs_by_each_period_duration(hours, mode):
    device = dp.DeviceParameters(.95, .9, 5., 5., 5., 5.)
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
