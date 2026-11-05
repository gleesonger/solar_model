"""Physical battery/inverter limits used by the optimisation model."""
import math
from dataclasses import dataclass
from collections.abc import Iterable

from solar_model.planning.types import DeviceInfo


@dataclass(frozen=True)
class SystemLimits:
    capacity_kwh: float
    max_charge_kwh: float
    max_discharge_kwh: float
    max_grid_import_kwh: float = float("inf")


def system_limits(device: DeviceInfo, interval_hours: float) -> SystemLimits:
    """Convert nameplate limits and efficiencies into one interval's limits."""
    if any(not math.isfinite(value) or not 0 < value <= 1 for value in (device.inverter_ac_to_dc_efficiency, device.inverter_dc_to_ac_efficiency, device.battery_charge_efficiency, device.battery_discharge_efficiency)):
        raise ValueError("conversion efficiencies must be finite, greater than zero and at most one")
    if math.isnan(device.grid_max_import_power_kw) or device.grid_max_import_power_kw < 0:
        raise ValueError("grid import limit must be non-negative")
    capacity = device.system_battery_capacity_kwh * device.battery_state_of_health_percent / 100
    max_charge_kwh = device.inverter_battery_charge_power_kw * interval_hours
    max_discharge_kwh = min(device.inverter_battery_discharge_power_kw, device.inverter_max_active_power_kw / device.inverter_dc_to_ac_efficiency) * interval_hours
    if capacity <= 0 or max_charge_kwh < 0 or max_discharge_kwh < 0:
        raise ValueError("battery capacity and charge/discharge limits must be non-negative")
    return SystemLimits(
        capacity,
        max_charge_kwh,
        max_discharge_kwh,
        device.grid_max_import_power_kw * interval_hours,
    )


def allocate_energy_routes(solar: Iterable[float], load: Iterable[float], solar_used: Iterable[float],
                           charge_input: Iterable[float], grid_charge_ac: Iterable[float], discharge_output: Iterable[float],
                           device: DeviceInfo, hours: Iterable[float]) -> dict[str, list[float]]:
    """Allocate solved port totals for reporting; these are not solver choices.

    PV supplies the household before battery charging, then grid export.
    Battery covers the remaining household demand before grid import. With exclusive
    battery directions this reproduces the signed inverter/grid balances.
    Input series must have matching lengths; each output column is a Python list.
    """
    routes: dict[str, list[float]] = {
        name: [] for name in ("solar_to_load", "solar_to_battery", "solar_to_grid", "solar_clipped",
            "grid_to_load", "grid_to_battery", "battery_to_load", "battery_to_grid",
            "battery_charge_loss", "battery_discharge_loss", "inverter_ac_to_dc_loss", "inverter_dc_to_ac_loss")
    }
    for available, demand, used, charge, grid_charge, discharge, period_hours in zip(solar, load, solar_used, charge_input, grid_charge_ac, discharge_output, hours, strict=True):
        solar_charge = charge - grid_charge * device.inverter_ac_to_dc_efficiency
        solar_output_ac = (used - solar_charge) * device.inverter_dc_to_ac_efficiency
        battery_output_ac = discharge * device.inverter_dc_to_ac_efficiency
        solar_load = min(solar_output_ac, demand)
        battery_load = min(battery_output_ac, max(0., demand - solar_load))
        values = {
            "solar_to_load": solar_load,
            "solar_to_battery": solar_charge * device.battery_charge_efficiency,
            "solar_to_grid": solar_output_ac - solar_load,
            "solar_clipped": available - used,
            "grid_to_load": demand - solar_load - battery_load,
            "grid_to_battery": grid_charge * device.inverter_ac_to_dc_efficiency * device.battery_charge_efficiency,
            "battery_to_load": battery_load,
            "battery_to_grid": battery_output_ac - battery_load,
            "battery_charge_loss": charge * (1 - device.battery_charge_efficiency),
            "battery_discharge_loss": discharge * (1 / device.battery_discharge_efficiency - 1),
            "inverter_ac_to_dc_loss": grid_charge * (1 - device.inverter_ac_to_dc_efficiency),
            "inverter_dc_to_ac_loss": (used - solar_charge + discharge) * (1 - device.inverter_dc_to_ac_efficiency),
        }
        for name, value in values.items():
            routes[name].append(value)
    return routes
