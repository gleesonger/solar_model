from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pymodbus.client import ModbusTcpClient

from common import LOGGER, source_path, timestamps
from config import TariffsConfig


REGISTER_MAP_PATH = source_path("sigen_register_map.json")
DEVICE_REGISTER_MAP_PATH = source_path("sigen_device_register_map.json")
MAX_REGISTERS_PER_BLOCK = 120


@dataclass(frozen=True)
class Register:
    metric: str
    address: int
    kind: str
    unit: str
    data_type: str = "u16"
    scale: float = 1.0
    cumulative: bool = False
    words: int = 1
    device_id: int | None = None


@dataclass(frozen=True)
class RegisterBlock:
    device_id: int
    kind: str
    address: int
    count: int
    registers: tuple[Register, ...]


def load_registers(
    path: str | Path,
    default_device_id: int,
    metrics: set[str] | None = None,
) -> list[RegisterBlock]:
    with open(path, encoding="utf-8") as file:
        definitions = json.load(file)
    if metrics is not None:
        available_metrics = {str(definition.get("metric")) for definition in definitions}
        missing_metrics = metrics - available_metrics
        if missing_metrics:
            raise ValueError(f"register map is missing metrics: {', '.join(sorted(missing_metrics))}")
        definitions = [
            definition for definition in definitions if definition.get("metric") in metrics
        ]
    registers = [Register(**definition) for definition in definitions]
    if not registers:
        raise ValueError(f"{path} contains no registers")
    registers = [
        register
        if register.device_id is not None
        else Register(**{**register.__dict__, "device_id": default_device_id})
        for register in registers
    ]
    if any(register.device_id is None for register in registers):
        raise ValueError("all registers must have a device ID")
    return group_registers(registers)


def group_registers(registers: list[Register]) -> list[RegisterBlock]:
    by_device_and_kind: dict[tuple[int, str], list[Register]] = {}
    for register in registers:
        if register.device_id is None:
            raise ValueError(f"register {register.metric} has no device_id")
        device_id = register.device_id
        by_device_and_kind.setdefault((device_id, register.kind), []).append(register)

    blocks: list[RegisterBlock] = []
    for (device_id, kind), definitions in by_device_and_kind.items():
        current: list[Register] = []
        current_end = -1
        for definition in sorted(definitions, key=lambda item: item.address):
            proposed_end = max(current_end, definition.address + definition.words)
            if current and proposed_end - current[0].address > MAX_REGISTERS_PER_BLOCK:
                blocks.append(RegisterBlock(device_id, kind, current[0].address, current_end - current[0].address, tuple(current)))
                current = []
            current.append(definition)
            current_end = max(current_end, definition.address + definition.words)
        if current:
            blocks.append(RegisterBlock(device_id, kind, current[0].address, current_end - current[0].address, tuple(current)))
    return sorted(blocks, key=lambda block: (block.kind, block.device_id, block.address))


def decode_registers(raw: list[int], data_type: str) -> int:
    value = 0
    for word in raw:
        value = (value << 16) | word
    bits = len(raw) * 16
    if data_type.startswith("s") and value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def decode_string(raw: list[int]) -> str:
    payload = b"".join(word.to_bytes(2, "big") for word in raw)
    return payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def read_block(client: ModbusTcpClient, kind: str, address: int, count: int, device_id: int):
    method = getattr(client, "read_holding_registers" if kind == "holding" else "read_input_registers")
    try:
        return method(address=address, count=count, **{"device_id": device_id})
    except TypeError:
        return method(address=address, count=count, **{"slave": device_id})


def read_registers(
    client: ModbusTcpClient,
    blocks: list[RegisterBlock],
) -> dict[str, tuple[float | str, str, bool]]:
    values: dict[str, tuple[float | str, str, bool]] = {}
    for block in blocks:
        result = read_block(client, block.kind, block.address, block.count, block.device_id)
        if result.isError():
            raise RuntimeError(f"Modbus read failed at {block.kind}:{block.address}: {result}")
        for definition in block.registers:
            offset = definition.address - block.address
            raw_value = result.registers[offset : offset + definition.words]
            if len(raw_value) != definition.words:
                raise RuntimeError(
                    f"short Modbus response for {definition.metric}: "
                    f"expected {definition.words} registers, got {len(raw_value)}"
                )
            value: float | str
            if definition.data_type == "string":
                value = decode_string(raw_value)
            else:
                value = decode_registers(raw_value, definition.data_type) / definition.scale
            values[definition.metric] = (value, definition.unit, definition.cumulative)
    return values


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
                )
                if changed:
                    LOGGER.info("device information updated: %s values", changed)
            except Exception:
                LOGGER.info("Device information collection failed; electrical sample retained")
        return True
    except Exception:
        LOGGER.info("Modbus interval failed; interval skipped")
        return False
