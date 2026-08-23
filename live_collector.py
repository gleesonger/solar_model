from __future__ import annotations

from pymodbus.client import ModbusTcpClient

from common import LOGGER, timestamps
from database import SolarDatabase
from modbus_collector import RegisterBlock, read_registers


LIVE_METRICS = {
    "plant_grid_power_kw",
    "plant_pv_power_kw",
    "plant_battery_power_kw",
    "plant_load_power_kw",
    "inverter_power_kw",
}


def collect_once(
    client: ModbusTcpClient,
    database: SolarDatabase,
    registers: list[RegisterBlock],
    timezone_name: str,
) -> bool:
    try:
        readings = read_registers(client, registers)
        values: dict[str, tuple[float, str, bool]] = {}
        for metric, (value, unit, cumulative) in readings.items():
            if isinstance(value, str):
                raise ValueError(f"live electrical metric {metric!r} returned text")
            values[metric] = (value, unit, cumulative)
        database.save_live_sample(values, timestamps(timezone_name=timezone_name))
        return True
    except Exception:
        LOGGER.info("Live Modbus interval failed; interval skipped")
        return False
