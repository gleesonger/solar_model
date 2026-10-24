from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Iterable
from enum import IntEnum

import pandas as pd
from solar_model.time_utils import utc_instant

@dataclass(frozen=True)
class SimulationResult:
    next_action: NextAction
    results: dict[str,pd.DataFrame]

class OperatingMode(IntEnum):
    SELF_CONSUMPTION = 2
    CHARGE_PV_FIRST = 4
    DISCHARGE_PV_FIRST = 5

@dataclass(frozen=True)
class NextAction:
    """Control for the next time step; forced battery rates in kW and SOC in percent."""
    mode: OperatingMode
    force_battery_charge_rate: float
    force_battery_discharge_rate: float
    charge_cut_off_soc_percent: float
    discharge_cut_off_soc_percent: float

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

@dataclass
class DeviceState:
    battery_kwh: float

@dataclass(frozen=True)
class ForecastPoint:
    solar_kwh: float
    load_kwh: float

@dataclass(frozen=True)
class ForecastInterval:
    starts_at: datetime
    ends_at: datetime
    kwh: float

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
        ordered = tuple(sorted(intervals, key=lambda interval: utc_instant(interval.starts_at)))
        if not ordered:
            raise ValueError(f"{name} forecast must contain at least one interval")
        for previous, current in zip(ordered, ordered[1:]):
            if utc_instant(current.starts_at) < utc_instant(previous.ends_at):
                raise ValueError(f"{name} forecast intervals must not overlap")
            if utc_instant(current.starts_at) > utc_instant(previous.ends_at):
                raise ValueError(f"{name} forecast intervals contain a gap")
        if any(utc_instant(interval.ends_at) <= utc_instant(interval.starts_at) for interval in ordered):
            raise ValueError(f"{name} forecast intervals must end after they start")
        return ordered

    @staticmethod
    def _sum_intervals(intervals: tuple[ForecastInterval, ...], start: datetime, end: datetime) -> float:
        start, end = utc_instant(start), utc_instant(end)
        cursor = start
        total_kwh = 0.0
        for interval in intervals:
            interval_start, interval_end = utc_instant(interval.starts_at), utc_instant(interval.ends_at)
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
