from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
MAX_REGISTERS_PER_BLOCK = 120
REGISTER_MAP_PATH = Path(__file__).with_name("register_maps") / "sigen_register_map.json"
DEVICE_REGISTER_MAP_PATH = Path(__file__).with_name("register_maps") / "sigen_device_register_map.json"

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
    variable_name: str | None = None

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
