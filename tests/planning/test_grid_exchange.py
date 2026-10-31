"""Grid billing uses one direction per interval without restricting site demand."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import cvxpy as cp
import pandas as pd
import pytest

from solar_model.config import TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.planning import plan_optimisation_milp as optimisation
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, ScenarioData


@pytest.fixture
def run_plan(monkeypatch):
    start = datetime(2026, 10, 29, 2, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    config = load_config()
    rule = TariffRule(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "00:00", "24:00", .0859)
    tariffs = TariffsConfig((TariffPeriod("2000-01-01", (rule,), (replace(rule, rate=.1716),)),))
    config = replace(config, tariffs=tariffs, energy_plan=replace(config.energy_plan, num_projection_hours=25, num_visible_projection_hours=1,
        optimiser_specific=replace(config.energy_plan.optimiser_specific, mix_integer_linear_prog=replace(config.energy_plan.optimiser_specific.mix_integer_linear_prog, projection_resolution_mins=((15,) * 4) + (240,) * 6)), protection_preference=0.))
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=18.08, battery_state_of_health_percent=100.,
        inverter_max_active_power_kw=5.5, inverter_max_absorption_power_kw=5.5,
        inverter_battery_charge_power_kw=8.8, inverter_battery_discharge_power_kw=9.6,
        charge_efficiency=.95, discharge_efficiency=.95)
    original_score = optimisation.score_simulation
    original_solve = cp.Problem.solve
    monkeypatch.setattr(cp.Problem, "solve", lambda problem, *args, **kwargs:
        original_solve(problem, *args, **{**kwargs, "time_limit": 5.}))

    def run(profiles, *, charge=0., discharge=0., initial=9., later_export=None, tail_load=0.):
        def score(costs, levels, scenarios, device, config, constraints, starts_at, durations):
            variables = {v.name(): v for constraint in constraints for v in constraint.variables()}
            constraints.extend([
                variables["next_force_battery_charge_rate"] == charge,
                variables["next_force_battery_discharge_rate"] == discharge,
                variables["next_charge_cut_off_soc_percent"] == 100.,
                variables["next_discharge_cut_off_soc_percent"] == 0.,
            ])
            if later_export is not None:
                constraints.append(variables["grid_export"][1, 0] == later_export)
            return original_score(costs, levels, scenarios, device, config, constraints, starts_at, durations)

        monkeypatch.setattr(optimisation, "score_simulation", score)
        scenarios = [ScenarioData(name, ForecastData(
            [ForecastInterval(start, end, solar), ForecastInterval(end, end + timedelta(hours=24), 0.)],
            [ForecastInterval(start, end, load), ForecastInterval(end, end + timedelta(hours=24), 24 * tail_load)]),
            1. / len(profiles)) for name, solar, load in profiles]
        return optimisation.simulate_energy_plan(device, DeviceState(initial), scenarios, config, start)

    return run


@pytest.mark.parametrize("load,charge,discharge,expected_import,expected_export", [
    (7., 0., 5.5, .375, 0.),  # Discharge reduces imports; it cannot earn export revenue.
    (.5, 0., 5.5, 0., 1.25),  # A real surplus still exports.
    (7., 5.5, 0., 3.125, 0.),  # Household demand bypasses the inverter limit.
])
def test_first_step_bills_only_net_grid_exchange(run_plan, load, charge, discharge,
                                               expected_import, expected_export):
    result = run_plan([("base", 0., load)], charge=charge, discharge=discharge)
    first = result.results["base"].iloc[0]
    assert first.grid_import_kwh == pytest.approx(expected_import, abs=1e-6)
    assert first.grid_export_kwh == pytest.approx(expected_export, abs=1e-6)
    assert first.net_cost == pytest.approx(expected_import * .0859 - expected_export * .1716)
    assert first.battery_end_kwh == pytest.approx(9. + charge * .25 * .95 - discharge * .25 / .95)
    for name, visible in result.results.items():
        complete = pd.concat([visible, result.lookahead_results[name]])
        assert ((complete.grid_import_kwh <= 1e-6) | (complete.grid_export_kwh <= 1e-6)).all()


def test_shared_battery_action_allows_opposite_grid_directions_across_scenarios(run_plan):
    result = run_plan([("dark", 0., 2.), ("sunny", 4., 2.)], discharge=1.)
    dark, sunny = (result.results[name].iloc[0] for name in ("dark", "sunny"))
    assert dark.grid_import_kwh == pytest.approx(.25)
    assert dark.grid_export_kwh == pytest.approx(0., abs=1e-6)
    assert sunny.grid_import_kwh == pytest.approx(0., abs=1e-6)
    assert sunny.grid_export_kwh == pytest.approx(.75)
    assert dark.battery_end_kwh == pytest.approx(sunny.battery_end_kwh)


def test_hidden_intervals_cannot_export_when_demand_exceeds_inverter_output(run_plan):
    result = run_plan([("base", 0., 7.)], tail_load=7.)
    hidden = result.lookahead_results["base"]
    assert len(hidden) == 6
    assert hidden.grid_export_kwh.tolist() == pytest.approx([0.] * 6, abs=1e-6)
    assert (hidden.grid_import_kwh > 0.).all()
    assert (hidden.net_cost > 0.).all()


def test_grid_charging_and_export_remain_possible_in_different_intervals(run_plan):
    result = run_plan([("base", 0., 0.)], charge=2., initial=0., later_export=.4)
    first, second = (result.results["base"].iloc[t] for t in (0, 1))
    assert first.grid_import_kwh == pytest.approx(.5)
    assert first.grid_export_kwh == pytest.approx(0., abs=1e-6)
    assert second.grid_import_kwh == pytest.approx(0., abs=1e-6)
    assert second.grid_export_kwh == pytest.approx(.4)
