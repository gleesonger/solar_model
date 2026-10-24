"""Physical battery/inverter limits used by the optimisation model."""
from dataclasses import dataclass

from solar_model.planning.types import DeviceInfo


@dataclass(frozen=True)
class SystemLimits:
    capacity_kwh: float
    max_charge_kwh: float
    max_discharge_kwh: float
    charge_loss_factor: float
    discharge_loss_factor: float


def system_limits(device: DeviceInfo, interval_hours: float) -> SystemLimits:
    """Convert nameplate limits and efficiencies into one interval's limits."""
    if not (0 < device.charge_efficiency <= 1) or not (0 < device.discharge_efficiency <= 1):
        raise ValueError("battery efficiencies must be between 0 and 1 (inclusive)")
    capacity = device.system_battery_capacity_kwh * device.battery_state_of_health_percent / 100
    charge = min(device.inverter_battery_charge_power_kw, device.inverter_max_absorption_power_kw) * interval_hours
    discharge = min(device.inverter_battery_discharge_power_kw, device.inverter_max_active_power_kw) * interval_hours
    if capacity <= 0 or charge < 0 or discharge < 0:
        raise ValueError("battery capacity and charge/discharge limits must be non-negative")
    return SystemLimits(capacity, charge, discharge,
                        1 / device.charge_efficiency - 1, 1 / device.discharge_efficiency - 1)
