"""Grid limit decoding, change history, UI and as-of planning contract."""
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solar_model.config import load_config
from solar_model.integrations.sigenstor.client import read_registers
from solar_model.integrations.sigenstor.registers import DEVICE_REGISTER_MAP_PATH, load_registers
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.system import system_limits
from solar_model.planning.types import DeviceInfo
from solar_model.storage.migrations import open_database
from solar_model.storage.queries.dashboard import load_device_information
from solar_model.storage.queries.planning import load_device_info_as_of


def test_grid_import_limit_history_and_as_of_selection(tmp_path):
    database = open_database(tmp_path / "test.db")
    config = load_config()
    metric = "Grid maximum import limitation"
    blocks = load_registers(DEVICE_REGISTER_MAP_PATH, 247, metrics={metric})
    register = blocks[0].registers[0]
    assert (blocks[0].kind, blocks[0].device_id, blocks[0].address, blocks[0].count) == ("holding", 247, 40040, 2)
    names = {metric: register.variable_name}
    client = Mock()
    try:
        # All old required metadata exists before collection of the new register.
        seed = {field.name: (1., "", False) for field in fields(DeviceInfo) if field.name != "grid_max_import_power_kw"}
        database.save_device_info(seed, ("2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"))
        def device_at(hour):
            return load_device_info_as_of(database, config, datetime(2026, 11, 1, hour, tzinfo=timezone.utc))
        assert device_at(0).grid_max_import_power_kw == float("inf")
        for hour, raw in [(1, 12345), (2, 0), (3, 6000)]:
            client.read_holding_registers.return_value = SimpleNamespace(isError=lambda: False, registers=[0, raw])
            readings = read_registers(client, blocks)
            assert readings[metric] == (raw / 1000, "kW", False)
            timestamp = f"2026-11-01T0{hour}:00:00+00:00"
            assert database.save_device_info(readings, (timestamp, timestamp), variable_names=names) == 1
            assert database.save_device_info(readings, (timestamp, timestamp), variable_names=names) == 0
        client.read_holding_registers.assert_called_with(address=40040, count=2, device_id=247)
        for hour, expected in [(1, 12.345), (2, 0.), (3, 6.)]:
            device = device_at(hour)
            assert device.grid_max_import_power_kw == expected
            assert system_limits(device, .25).max_grid_import_kwh == pytest.approx(expected / 4)
            assert dp.system_limits(device, .25).max_grid_import_kwh == pytest.approx(expected / 4)
        assert {"variable": metric, "value": "6", "unit": "kW"} in load_device_information(str(tmp_path / "test.db"))
        assert device_at(0).grid_max_import_power_kw == float("inf")
    finally:
        database.close()
