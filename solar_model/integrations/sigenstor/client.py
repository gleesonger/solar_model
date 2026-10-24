from __future__ import annotations
from pymodbus.client import ModbusTcpClient
from solar_model.integrations.sigenstor.registers import RegisterBlock, decode_registers, decode_string

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
