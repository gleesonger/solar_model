from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Iterable
from enum import IntEnum

import pandas as pd
from solar_model.config import SolarArrayConfig
from solar_model.time_utils import utc_instant

@dataclass(frozen=True)
class SimulationResult:
    next_action: NextAction
    results: dict[str,pd.DataFrame]
    lookahead_results: dict[str, pd.DataFrame] = field(default_factory=dict)

class NoFeasiblePlanError(ValueError):
    """The planner could not find a feasible plan on its search grid."""


class OperatingMode(IntEnum):
    SELF_CONSUMPTION = 2
    CHARGE_PV_FIRST = 4
    DISCHARGE_PV_FIRST = 5

class PlanCommand(IntEnum):
    """Program commands, distinct from Sigenergy's remote EMS mode numbers."""
    SELF_CONSUMPTION = 0    # PV serves the house with surplus to the battery. Load shortfalls meet by the battery before falling back to grid.
    SOLAR_CHARGE_ONLY = 1   # PV serves the house then charges the battery; no battery discharge.
    HOLD_BATTERY = 2        # No battery flow; PV/grid supply the house and surplus PV exports.
    SOLAR_EXPORT_FIRST = 5  # PV serves house/exports first; only inverter overflow charges the battery, with no discharge.
    
    FORCE_CHARGE = 3        # Fill battery from solar+grid
    FORCE_DISCHARGE = 4     # Empty the battery to load+grid

    @property
    def label(self) -> str:
        return {
            PlanCommand.SELF_CONSUMPTION: "Self-consumption",
            PlanCommand.SOLAR_CHARGE_ONLY: "Solar charge only",
            PlanCommand.HOLD_BATTERY: "Hold battery",
            PlanCommand.FORCE_CHARGE: "Force charge",
            PlanCommand.FORCE_DISCHARGE: "Force discharge",
            PlanCommand.SOLAR_EXPORT_FIRST: "Solar export first",
        }[self]


@dataclass(frozen=True)
class NextAction:
    """Shared next command; battery power ceilings in kW and SOC stops in percent.

    Achieved first-interval power can differ between scenarios due to PV,
    equipment saturation and commanded SOC stops. User limits constrain the
    command and its response; this object does not execute EMS.

    DP specifies command_type and both global battery power limits explicitly.
    Legacy MILP results leave those fields unset; command resolves their mode.
    """
    mode: OperatingMode
    force_battery_charge_rate: float
    force_battery_discharge_rate: float
    charge_cut_off_soc_percent: float
    discharge_cut_off_soc_percent: float
    command_type: PlanCommand | None = None
    charge_power_limit_kw: float | None = None
    discharge_power_limit_kw: float | None = None

    @property
    def command(self) -> PlanCommand:
        """Resolve legacy MILP results as well as explicit DP command profiles."""
        if self.command_type is not None:
            return self.command_type
        return {
            OperatingMode.SELF_CONSUMPTION: PlanCommand.SELF_CONSUMPTION,
            OperatingMode.CHARGE_PV_FIRST: PlanCommand.FORCE_CHARGE,
            OperatingMode.DISCHARGE_PV_FIRST: PlanCommand.FORCE_DISCHARGE,
        }[self.mode]

@dataclass
class DeviceInfo:
    inverter_model: str
    inverter_serial_number: str
    inverter_firmware: str
    inverter_rated_power_kw: float
    inverter_max_apparent_power_kva: float
    inverter_max_active_power_kw: float
    inverter_max_absorption_power_kw: float
    inverter_battery_charge_power_kw: float
    inverter_battery_discharge_power_kw: float
    rated_grid_voltage_v: float
    rated_grid_frequency_hz: float
    battery_pack_count: float
    pv_string_count: float
    mppt_count: float
    system_battery_capacity_kwh: float
    battery_charge_cut_off_soc_percent: float
    battery_discharge_cut_off_soc_percent: float
    battery_state_of_health_percent: float
    maximum_pv_input_power_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    grid_max_import_power_kw: float = float("inf")  # Unknown in device history predating collection.

@dataclass
class DeviceState:
    battery_kwh: float

@dataclass(frozen=True)
class ForecastPoint:
    solar_kwh: float
    load_kwh: float

@dataclass(frozen=True)
class ForecastInterval:
    """Forecast energy over an aware interval, normalised to UTC at entry."""
    starts_at: datetime
    ends_at: datetime
    kwh: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "starts_at", utc_instant(self.starts_at))
        object.__setattr__(self, "ends_at", utc_instant(self.ends_at))

class ForecastData:
    def __init__(self, solar: Iterable[ForecastInterval], load: Iterable[ForecastInterval]) -> None:
        self.solar = self._validate_intervals(solar, "solar")
        self.load = self._validate_intervals(load, "load")

    def sum(self, start_time: datetime, end_time: datetime) -> ForecastPoint:
        start = utc_instant(start_time)
        end = utc_instant(end_time)
        if end <= start:
            raise ValueError("forecast end_time must be after start_time")

        return ForecastPoint(solar_kwh=self._sum_intervals(self.solar, start, end), load_kwh=self._sum_intervals(self.load, start, end))

    @staticmethod
    def _validate_intervals(intervals: Iterable[ForecastInterval], name: str) -> tuple[ForecastInterval, ...]:
        ordered = tuple(sorted(intervals, key=lambda interval: interval.starts_at))
        if not ordered:
            raise ValueError(f"{name} forecast must contain at least one interval")
        for previous, current in zip(ordered, ordered[1:]):
            if current.starts_at < previous.ends_at:
                raise ValueError(f"{name} forecast intervals must not overlap")
            if current.starts_at > previous.ends_at:
                raise ValueError(f"{name} forecast intervals contain a gap")
        if any(interval.ends_at <= interval.starts_at for interval in ordered):
            raise ValueError(f"{name} forecast intervals must end after they start")
        return ordered

    @staticmethod
    def _sum_intervals(intervals: tuple[ForecastInterval, ...], start: datetime, end: datetime) -> float:
        cursor = start
        total_kwh = 0.0
        for interval in intervals:
            interval_start, interval_end = interval.starts_at, interval.ends_at
            if interval_end <= cursor:
                continue
            if interval_start > cursor:
                break
            overlap_start = max(cursor, interval_start)
            overlap_end = min(end, interval_end)
            if overlap_end > overlap_start:
                interval_seconds = (interval_end - interval_start).total_seconds()
                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                total_kwh += interval.kwh * overlap_seconds / interval_seconds
                cursor = overlap_end
            if cursor >= end:
                return total_kwh
        raise ValueError("forecast range is outside the known data or contains a gap; extrapolation is not supported")


@dataclass(frozen=True)
class ScenarioData:
    name: str
    forecast_data: ForecastData
    probability: float
