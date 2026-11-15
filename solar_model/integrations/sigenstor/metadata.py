"""Status codes from Sigenergy Modbus V2.9; stored values remain numeric text."""
from __future__ import annotations


DEVICE_STATUS_LABELS: dict[str, dict[str, str]] = {
    "EMS work mode": {
        "0": "Maximum self-consumption",
        "1": "AI mode",
        "2": "TOU",
        "5": "Full feed-in to grid",
        "7": "Remote EMS",
        "9": "Custom",
    },
    "Inverter output type": {"0": "L/N", "1": "L1/L2/L3", "2": "L1/L2/L3/N", "3": "L1/L2/N"},
    "Inverter running state": {"0": "Standby", "1": "Running", "2": "Fault", "3": "Shutdown", "7": "Environmental abnormality"},
}
PLANT_ALARM_METRICS: frozenset[str] = frozenset(f"Plant alarm {number}" for number in range(1, 7))
ZERO_VALID_DEVICE_METRICS: frozenset[str] = frozenset(DEVICE_STATUS_LABELS) | PLANT_ALARM_METRICS | {"Grid maximum import limitation"}


def device_display_value(variable: str, value: str) -> str:
    label = DEVICE_STATUS_LABELS.get(variable, {}).get(value)
    if label is not None:
        return f"{value} ({label})"
    if variable in PLANT_ALARM_METRICS:
        try:
            word = int(value)
        except ValueError:
            return value
        if 0 <= word <= 0xFFFF:
            return f"0x{word:04X}" + (" (none)" if word == 0 else "")
    return value
