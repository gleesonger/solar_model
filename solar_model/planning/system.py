"""Physical battery/inverter limits used by the optimisation model."""
from dataclasses import dataclass
from collections.abc import Iterable

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
    max_charge_kwh = device.inverter_battery_charge_power_kw * interval_hours
    max_discharge_kwh = min(device.inverter_battery_discharge_power_kw, device.inverter_max_active_power_kw) * interval_hours
    if capacity <= 0 or max_charge_kwh < 0 or max_discharge_kwh < 0:
        raise ValueError("battery capacity and charge/discharge limits must be non-negative")
    return SystemLimits(capacity, max_charge_kwh, max_discharge_kwh,
                        1 / device.charge_efficiency - 1, 1 / device.discharge_efficiency - 1)


def allocate_energy_routes(solar: Iterable[float], load: Iterable[float], solar_used: Iterable[float],
                           charge_input: Iterable[float], discharge_output: Iterable[float],
                           device: DeviceInfo) -> dict[str, list[float]]:
    """Allocate solved port totals for reporting; these are not solver choices.

    Charging uses PV before grid energy. Remaining PV supplies the household
    before battery discharge; surplus output is exported. With exclusive
    battery directions this reproduces the signed inverter/grid balances.
    Input series must have matching lengths; each output column is a Python list.
    """
    routes: dict[str, list[float]] = {
        name: [] for name in ("solar_to_load", "solar_to_battery", "solar_to_battery_loss",
            "grid_to_battery", "grid_to_battery_loss", "solar_to_grid", "solar_clipped",
            "battery_to_load", "battery_to_grid", "battery_to_ac_loss", "grid_to_load")
    }
    for available, demand, used, charge, discharge in zip(solar, load, solar_used, charge_input, discharge_output, strict=True):
        solar_charge = min(used, charge)
        grid_charge = charge - solar_charge
        solar_output = used - solar_charge
        solar_load = min(solar_output, demand)
        battery_load = min(discharge, max(0., demand - solar_load))
        values = {
            "solar_to_load": solar_load,
            "solar_to_battery": solar_charge * device.charge_efficiency,
            "solar_to_battery_loss": solar_charge * (1 - device.charge_efficiency),
            "grid_to_battery": grid_charge * device.charge_efficiency,
            "grid_to_battery_loss": grid_charge * (1 - device.charge_efficiency),
            "solar_to_grid": solar_output - solar_load,
            "solar_clipped": available - used,
            "battery_to_load": battery_load,
            "battery_to_grid": discharge - battery_load,
            "battery_to_ac_loss": discharge * (1 / device.discharge_efficiency - 1),
            "grid_to_load": demand - solar_load - battery_load,
        }
        for name, value in values.items():
            routes[name].append(value)
    return routes
