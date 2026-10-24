from __future__ import annotations
from pymodbus.client import ModbusTcpClient
from solar_model.config import TariffsConfig
from solar_model.logging_setup import LOGGER, heavy_work
from solar_model.time_utils import timestamps
from solar_model.integrations.sigenstor.registers import RegisterBlock
from solar_model.integrations.sigenstor.client import read_registers

@heavy_work("full Modbus data collection update")
def collect_once(
    client: ModbusTcpClient,
    database,
    registers: list[RegisterBlock],
    previous: dict[str, float],
    timezone_name: str,
    tariffs: TariffsConfig,
    device_registers: list[RegisterBlock] | None = None,
) -> bool:
    try:
        readings = read_registers(client, registers)
        values: dict[str, tuple[float, str, bool]] = {}
        for metric, (value, unit, cumulative) in readings.items():
            if isinstance(value, str):
                raise ValueError(f"electrical metric {metric!r} returned text")
            values[metric] = (value, unit, cumulative)
        collected = timestamps(timezone_name=timezone_name)
        database.save_modbus_sample(values, previous, collected, tariffs)
        power = {metric: reading[0] for metric, reading in values.items() if metric in {
            "plant_pv_power_kw", "plant_load_power_kw", "plant_battery_power_kw", "plant_grid_power_kw"
        }}
        grid = power.get("plant_grid_power_kw", 0.0)
        LOGGER.info(
            "power kW: solar=%s load=%s battery=%s grid-import=%s grid-export=%s",
            power.get("plant_pv_power_kw"),
            power.get("plant_load_power_kw"),
            power.get("plant_battery_power_kw"),
            max(grid, 0.0),
            max(-grid, 0.0),
        )
        if device_registers is not None:
            try:
                changed = database.save_device_info(
                    read_registers(client, device_registers),
                    collected,
                    variable_names={
                        register.metric: register.variable_name
                        for block in device_registers
                        for register in block.registers
                        if register.variable_name
                    } | {"Maximum PV input power": "maximum_pv_input_power_kw"},
                )
                if changed:
                    LOGGER.info("device information updated: %s values", changed)
            except Exception:
                LOGGER.info("Device information collection failed; electrical sample retained")
        return True
    except Exception:
        LOGGER.info("Modbus interval failed; interval skipped")
        return False
