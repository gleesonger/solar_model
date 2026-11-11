"""Shared Markov physical transitions, command profiles and report checks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.types import OperatingMode
from tests.planning.helpers import inputs


def period(**changes):
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    base = dp.Period(start.timestamp(), (start + timedelta(minutes=30)).timestamp(), 0., .5, .4, .1, "flat", 0., 10., 9999., .5, 0., 4.)
    return base._replace(**{name: value if name == "tariff_name" else float(value) for name, value in changes.items()})


def test_epoch_periods_preserve_fractional_utc_report_timestamps(inputs):
    start = inputs[-1].replace(microsecond=123456)
    end = start + timedelta(minutes=30)
    p = period(starts_at=start.timestamp(), ends_at=end.timestamp())
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, 2., dp.Action(OperatingMode.SELF_CONSUMPTION, 0.))
    frame = dp.result_frame(inputs[0], [p], [step], 2., .2)
    assert frame.starts_at.iloc[0] == start
    assert frame.ends_at.iloc[0] == end
    assert str(frame.starts_at.dt.tz) == "UTC"
    assert frame.tariff_name.tolist() == ["flat"]


def test_dp_aligns_first_hour_and_preserves_elapsed_horizon_through_dst(inputs):
    config = replace(inputs[3], energy_plan=replace(inputs[3].energy_plan, num_projection_hours=24, num_visible_projection_hours=24, optimiser=replace(inputs[3].energy_plan.optimiser, projection_resolution_mins=(15,) * 96)))
    start = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)
    intervals = dp.projection_intervals(config, start)
    assert len(intervals) == 97
    assert intervals[0][0] == start
    assert intervals[-1][1] == start + timedelta(hours=24)
    assert [(end - begin).total_seconds() / 60 for begin, end in intervals] == [15, 10] + [15] * 94 + [5]


def test_actions_span_minimum_to_maximum_and_interpolation_keeps_physical_energy_separate(inputs):
    choices = dp.actions(inputs[0], inputs[3].energy_plan)
    assert len(choices) == 13
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx([0., .88, 1.66, 2.44, 3.22, 4.])
    assert dp.interpolate([0., 1.], [0., 10.], .37) == pytest.approx(3.7)
    assert dp.interpolate([0., 1.], [-float("inf"), 10.], .37) == -float("inf")


def test_tenths_option_generates_twenty_four_commands(inputs):
    choices = dp.actions(inputs[0], inputs[3].energy_plan, power_divisions=10)
    assert len(choices) == 23
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        rates = [action.rate_kw for action in choices if action.mode == mode]
        assert rates == pytest.approx([0., .49, .88, 1.27, 1.66, 2.05, 2.44, 2.83, 3.22, 3.61, 4.])


@pytest.mark.parametrize("energy,expected", [(0., 0.), (2., 100 * (1 - .3 ** .5)), (4., 70.), (8., 80.)])
def test_transition_uses_exact_protection_curve(inputs, energy, expected):
    p = period(solar=0., load=0., protection_weight=.2, preferred_reserve_kwh=4.)
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, energy, dp.Action(OperatingMode.SELF_CONSUMPTION, 0.))
    assert step.protection_score == pytest.approx(.2 * expected, abs=1e-12)
    assert step.score == pytest.approx(step.protection_score - step.net_cost)


@pytest.mark.parametrize("maximum,expected", [(.05, []), (.1, [.1]), (.15, [.11, .12, .13, .14, .15])])
def test_minimum_power_choices_are_unique_and_within_hardware_limits(inputs, maximum, expected):
    device = replace(inputs[0], inverter_battery_charge_power_kw=maximum, inverter_battery_discharge_power_kw=maximum)
    choices = dp.actions(device, inputs[3].energy_plan)
    assert choices[0] == dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx([0., *expected])


def test_charge_and_discharge_use_separate_minimum_power(inputs):
    settings = replace(inputs[3].energy_plan, min_forced_charge_power_kw=1., min_forced_discharge_power_kw=2.)
    choices = dp.actions(inputs[0], settings)
    assert [action.rate_kw for action in choices if action.mode == OperatingMode.CHARGE_PV_FIRST] == pytest.approx([0., 1.6, 2.2, 2.8, 3.4, 4.])
    assert [action.rate_kw for action in choices if action.mode == OperatingMode.DISCHARGE_PV_FIRST] == pytest.approx([0., 2.4, 2.8, 3.2, 3.6, 4.])


@pytest.mark.parametrize("maximum,expected", [(1., [.28, .46, .64, .82, 1.]), (999., [.88, 1.66, 2.44, 3.22, 4.]), (.05, [])])
def test_forced_power_maximum_caps_choices_or_disables_mode(inputs, maximum, expected):
    settings = replace(inputs[3].energy_plan, max_forced_charge_power_kw=maximum, max_forced_discharge_power_kw=maximum)
    choices = dp.actions(inputs[0], settings)
    assert choices[0] == dp.Action(OperatingMode.SELF_CONSUMPTION, 0.)
    for mode in (OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST):
        assert [action.rate_kw for action in choices if action.mode == mode] == pytest.approx([0., *expected])


@pytest.mark.parametrize("mode,minimum,maximum,expected", [
    (OperatingMode.CHARGE_PV_FIRST, 0., 2.5, 4.),
    (OperatingMode.DISCHARGE_PV_FIRST, 1.5, 10., 0.),
])
def test_scheduled_bounds_are_scored_without_stopping_the_command(inputs, mode, minimum, maximum, expected):
    p = period(minimum=minimum, maximum=maximum)
    action = (mode, 4.)
    step = dp.simulate_system_single_time_period(inputs[0], 10., p, 2., action)
    command = dp.command(action, inputs[0])
    assert step.battery_end == pytest.approx(expected)
    assert step.limit_score < 0
    assert command.target_soc_percent is None


def test_self_consumption_scores_user_bounds_and_export_limits_stay_hard(inputs):
    device = inputs[0]
    step = dp.simulate_system_single_time_period(device, 10., period(minimum=1.8), 2., (OperatingMode.SELF_CONSUMPTION, 0.))
    assert step.battery_end == pytest.approx(1.5)
    assert step.limit_score < 0
    assert dp.simulate_system_single_time_period(device, 10., period(minimum=1.5, export_limit=0.), 2., (OperatingMode.DISCHARGE_PV_FIRST, 4.)) is None
    step = dp.simulate_system_single_time_period(device, 10., period(export_limit=0.), 2., (OperatingMode.DISCHARGE_PV_FIRST, 1.))
    assert step.discharge == pytest.approx(.5)
    assert step.grid_export == pytest.approx(0.)


@pytest.mark.parametrize("energy,expected", [(4., 0.), (6., 0.), (5., 0.), (3.99, -100 / 2501), (3.9, -100 / 26), (6.5, -50.), (7., -80.), (8., -1600 / 17)])
def test_limit_penalty_is_bounded_symmetric_and_duration_weighted(energy, expected):
    score = dp.battery_limit_score(energy, 4., 6., 10., 1.)
    assert score == pytest.approx(expected)
    assert -100 < score <= 0
    assert dp.battery_limit_score(energy, 4., 6., 10., .25) == pytest.approx(score / 4)
    assert dp.battery_limit_score(energy * 2, 8., 12., 20., 1.) == pytest.approx(score)


@pytest.mark.parametrize("breach,expected", [(1e-300, 0.), (1e300, -100.)])
def test_limit_penalty_remains_finite_for_extreme_breaches(breach, expected):
    assert dp.battery_limit_score(0., breach, breach, 10., 1.) == pytest.approx(expected)


@pytest.mark.parametrize("mode,initial,expected", [
    (OperatingMode.CHARGE_PV_FIRST, 9.5, 10.),
    (OperatingMode.DISCHARGE_PV_FIRST, .5, 0.),
])
def test_soft_bounds_preserve_physical_battery_capacity(inputs, mode, initial, expected):
    step = dp.simulate_system_single_time_period(inputs[0], 10., period(minimum=4., maximum=6.), initial, dp.Action(mode, 4.))
    assert step.battery_end == expected
    assert step.limit_score < 0


@pytest.mark.parametrize("kind", [OperatingMode.CHARGE_PV_FIRST, OperatingMode.DISCHARGE_PV_FIRST])
@pytest.mark.parametrize("solar,load", [(0., .5), (.2, .8), (1.5, .5), (5., 0.)])
def test_zero_power_variants_use_grid_for_shortfalls_and_only_solar_can_charge(kind, solar, load):
    device = dp.DeviceParameters(1., 1., .95, .9, 4., 4., 4., 4.)
    p = period(solar=solar, load=load)
    step = dp.simulate_system_single_time_period(device, 10., p, 2., dp.Action(kind, 0.))
    expected_charge = min(max(0., solar - min(solar, load, 2.)), 2.) if kind == OperatingMode.DISCHARGE_PV_FIRST else 0.
    assert step.mode == kind
    assert step.charge == pytest.approx(expected_charge)
    assert step.discharge == 0
    assert step.battery_end == pytest.approx(2. + expected_charge * .95)
    assert step.grid_import == pytest.approx(max(0., load - min(solar, 2.)))
    assert step.solar_used <= solar
    assert step.solar_used - step.charge - load == pytest.approx(step.grid_export - step.grid_import)


@pytest.mark.parametrize("mode,rate,charge,discharge", [
    (OperatingMode.SELF_CONSUMPTION, 0., 4., 4.),
    (OperatingMode.CHARGE_PV_FIRST, 0., 0., 0.),
    (OperatingMode.CHARGE_PV_FIRST, 1., 1., 0.),
    (OperatingMode.DISCHARGE_PV_FIRST, 0., 4., 0.),
    (OperatingMode.DISCHARGE_PV_FIRST, 1., 4., 1.),
])
def test_modes_define_complete_modbus_power_profiles(inputs, mode, rate, charge, discharge):
    profile = dp.command(dp.Action(mode, rate), inputs[0])
    assert profile.mode == mode
    assert profile.charge_power_limit_kw == charge
    assert profile.discharge_power_limit_kw == discharge
    assert profile.force_battery_charge_rate == (rate if mode == OperatingMode.CHARGE_PV_FIRST else 0.)
    assert profile.force_battery_discharge_rate == (rate if mode == OperatingMode.DISCHARGE_PV_FIRST else 0.)


def test_grouped_cli_reports_preserve_distinct_selected_commands(inputs):
    from solar_model.tools.reports import group_plan

    first = period(solar=1., load=.5)
    second = first._replace(starts_at=first.ends_at, ends_at=first.ends_at + 1800.)
    solar = dp.simulate_system_single_time_period(inputs[0], 10., first, 2., dp.Action(OperatingMode.DISCHARGE_PV_FIRST, 0.))
    hold = dp.simulate_system_single_time_period(inputs[0], 10., second, solar.battery_end, dp.Action(OperatingMode.CHARGE_PV_FIRST, 0.))
    frame = dp.result_frame(inputs[0], [first, second], [solar, hold], 2., .1)
    assert frame.plan_action.tolist() == ["Force discharge", "Force charge"]
    assert group_plan(frame, 60).plan_action.tolist() == ["Force discharge / Force charge"]


@pytest.mark.parametrize("solar,load,initial,charge,used,imported,exported", [
    (.5, 1., 2., 0., .5, .5, 0.),
    (1.5, .5, 2., 1., 1.5, 0., 0.),
    (3., .5, 2., 2., 3., 0., .5),
    (5., .5, 2., 2., 4., 0., 1.5),
    (5., .5, 10., 0., 2., 0., 1.5),
    (5., .5, 9.9, .1 / .95, 2. + .1 / .95, 0., 1.5),
])
def test_zero_discharge_charges_surplus_before_export(solar, load, initial, charge, used, imported, exported):
    device = dp.DeviceParameters(1., 1., .95, .9, 4., 4., 4., 4.)
    step = dp.simulate_system_single_time_period(device, 10., period(solar=solar, load=load), initial, dp.Action(OperatingMode.DISCHARGE_PV_FIRST, 0.))
    assert step.charge == pytest.approx(charge)
    assert step.discharge == 0
    assert step.solar_used == pytest.approx(used)
    assert step.grid_import == pytest.approx(imported)
    assert step.grid_export == pytest.approx(exported)
    assert step.battery_end == pytest.approx(initial + .95 * charge)
    assert step.solar_used - charge - load == pytest.approx(exported - imported)


@pytest.mark.parametrize("hours", [.25, 1., 4.])
@pytest.mark.parametrize("solar_kw", [0., 1., 5.])
def test_grid_import_limit_reduces_forced_charging_after_household_demand(inputs, hours, solar_kw):
    device = replace(inputs[0], grid_max_import_power_kw=2.)
    p = period(hours=hours, solar=solar_kw * hours, load=hours)
    step = dp.simulate_system_single_time_period(device, 100., p, 0., dp.Action(OperatingMode.CHARGE_PV_FIRST, 4.))
    assert step is not None
    assert step.grid_import <= 2. * hours + 1e-9
    assert step.charge == pytest.approx(min(4., solar_kw + 1.) * hours)
    assert step.solar_used + step.grid_import == pytest.approx(step.charge + hours + step.grid_export)


@pytest.mark.parametrize("command", list(OperatingMode))
def test_grid_import_limit_rejects_unserved_household_demand(inputs, command):
    device = replace(inputs[0], grid_max_import_power_kw=1.)
    assert dp.simulate_system_single_time_period(device, 10., period(load=2.), 0., dp.Action(command, 4.)) is None


def test_zero_grid_import_limit_allows_battery_and_solar_supply(inputs):
    device = replace(inputs[0], grid_max_import_power_kw=0.)
    p = period(load=.5)
    step = dp.simulate_system_single_time_period(device, 10., p, 2., dp.Action(OperatingMode.SELF_CONSUMPTION, 0.))
    assert step is not None and step.grid_import == 0.
    assert step.discharge == pytest.approx(.5)
    step = dp.simulate_system_single_time_period(device, 10., p._replace(solar=2.), 0., dp.Action(OperatingMode.CHARGE_PV_FIRST, 4.))
    assert step is not None and step.grid_import == 0.
    assert step.charge == pytest.approx(1.5)


@pytest.mark.parametrize("limit", [-1., float("nan"), -float("inf")])
def test_grid_import_limit_validation(inputs, limit):
    with pytest.raises(ValueError, match="grid import limit"):
        dp.system_limits(replace(inputs[0], grid_max_import_power_kw=limit), 1.)
