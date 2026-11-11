"""Independent conservation and scoring checks for the system-port formulation."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.system import allocate_energy_routes
from solar_model.planning.types import ForecastData, ScenarioData
from tests.planning.helpers import inputs


def test_report_allocations_preserve_port_energy_and_net_meter_exchange(inputs):
    device = replace(inputs[0], inverter_ac_to_dc_efficiency=.9, inverter_dc_to_ac_efficiency=.8,
                     battery_charge_efficiency=.7, battery_discharge_efficiency=.6)
    solar = np.array([3., 3., 0.])
    used = np.array([3., 2., 0.])
    load = np.array([1., 2., 1.])
    charge = np.array([1., 0., .9])
    grid_charge = np.array([0., 0., 1.])
    discharge = np.array([0., 1., 0.])
    flows = allocate_energy_routes(solar, load, used, charge, grid_charge, discharge, device, [1.] * len(solar))
    # Lists are the reporting contract; arrays here independently check balances.
    assert all(isinstance(values, list) for values in flows.values())
    flows = {name: np.asarray(values) for name, values in flows.items()}
    assert all((value >= 0.).all() for value in flows.values())
    np.testing.assert_allclose(solar, (flows["solar_to_load"] + flows["solar_to_grid"]) / .8
                               + flows["solar_to_battery"] / .7 + flows["solar_clipped"])
    np.testing.assert_allclose(load, flows["solar_to_load"] + flows["battery_to_load"] + flows["grid_to_load"])
    np.testing.assert_allclose(.7 * charge - discharge / .6,
        flows["solar_to_battery"] + flows["grid_to_battery"]
        - (flows["battery_to_load"] + flows["battery_to_grid"]) / (.8 * .6))
    imports = flows["grid_to_load"] + grid_charge
    exports = flows["solar_to_grid"] + flows["battery_to_grid"]
    np.testing.assert_allclose(exports - imports, .8 * (used - charge + .9 * grid_charge + discharge) - grid_charge - load)
    total_loss = sum(flows[name] for name in ("battery_charge_loss", "battery_discharge_loss", "inverter_ac_to_dc_loss", "inverter_dc_to_ac_loss"))
    np.testing.assert_allclose(solar + imports + discharge / .6 - load - exports - charge * .7 - flows["solar_clipped"], total_loss)
