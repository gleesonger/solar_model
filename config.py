from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml
import dacite

@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class DatabaseConfig:
    path: str


@dataclass(frozen=True)
class DataRetrivalScheduleConfig:
    live_update_interval_seconds: float
    full_updated_interval_seconds: int


@dataclass(frozen=True)
class SolarArrayConfig:
    panel_id: int
    name: str
    latitude: float
    longitude: float
    declination: float
    azimuth: float
    peak_kw: float


@dataclass(frozen=True)
class ForecastConfig:
    interval_seconds: int
    timeout_seconds: float
    endpoint: str
    arrays: tuple[SolarArrayConfig, ...]


@dataclass(frozen=True)
class EnergyPlanConfig:
    battery_target_soc_percent: float
    battery_minimum_soc_percent: float
    charge_efficiency: float
    discharge_efficiency: float


@dataclass(frozen=True)
class DashboardConfig:
    port: int
    host: str
    actuals_to_forecast: dict[str, int]


@dataclass(frozen=True)
class TariffRule:
    days: tuple[str, ...]
    start_time: str
    end_time: str
    rate: float
    name: str | None = None

    @property
    def start_minutes(self) -> int:
        return tariff_time_minutes(self.start_time)

    @property
    def end_minutes(self) -> int:
        return tariff_time_minutes(self.end_time, allow_end_of_day=True)


@dataclass(frozen=True)
class TariffPeriod:
    effective_from: str
    import_: tuple[TariffRule, ...]
    export: tuple[TariffRule, ...]


@dataclass(frozen=True)
class TariffsConfig:
    periods: tuple[TariffPeriod, ...]


@dataclass(frozen=True)
class ModbusConfig:
    host: str
    port: int
    timeout_seconds: float
    default_device_id: int


@dataclass(frozen=True)
class ActualsConfig:
    data_retrival_schedule: DataRetrivalScheduleConfig
    modbus: ModbusConfig


@dataclass(frozen=True)
class Config:
    timezone: str
    logging: LoggingConfig
    database: DatabaseConfig
    forecast: ForecastConfig
    energy_plan: EnergyPlanConfig
    dashboard: DashboardConfig
    tariffs: TariffsConfig
    actuals: ActualsConfig



def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as file:
        raw_config = yaml.safe_load(file)

    try:
        tariff_data = raw_config.get("tariffs") if isinstance(raw_config, dict) else None
        if isinstance(tariff_data, dict) and isinstance(tariff_data.get("periods"), list):
            for period in tariff_data["periods"]:
                if isinstance(period, dict) and "import" in period:
                    period["import_"] = period.pop("import")
        config = dacite.from_dict(
            data_class=Config,
            data=raw_config,
            config=dacite.Config(
                cast=[tuple],
            ),
        )
        validate_config(config)
        return config
    except (dacite.DaciteError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error


def validate_config(config: Config) -> None:
    """Validate configuration values after the YAML has been deserialized."""
    if (
        config.actuals.data_retrival_schedule.live_update_interval_seconds <= 0
        or config.actuals.data_retrival_schedule.full_updated_interval_seconds <= 0
        or config.forecast.interval_seconds <= 0
    ):
        raise ValueError("scheduler, live, and forecast intervals must be greater than zero")

    if config.forecast.timeout_seconds <= 0:
        raise ValueError("forecast timeout must be greater than zero")

    energy_plan = config.energy_plan
    if not 0 <= energy_plan.battery_minimum_soc_percent <= energy_plan.battery_target_soc_percent <= 100:
        raise ValueError("energy_plan battery SoC bounds must satisfy 0 <= minimum <= target <= 100")
    if not 0 < energy_plan.charge_efficiency <= 1 or not 0 < energy_plan.discharge_efficiency <= 1:
        raise ValueError("energy_plan battery efficiencies must be greater than zero and at most one")

    try:
        ZoneInfo(config.timezone)
    except KeyError as error:
        raise ValueError(f"timezone is not valid: {config.timezone}") from error

    if config.logging.level not in logging.getLevelNamesMapping():
        raise ValueError(f"logging.level is not valid: {config.logging.level}")

    if not config.tariffs.periods:
        raise ValueError("tariffs.periods must contain at least one period")
    previous_date: date | None = None
    for period in config.tariffs.periods:
        try:
            effective_date = date.fromisoformat(period.effective_from)
        except ValueError as error:
            raise ValueError(f"tariff effective_from must use YYYY-MM-DD: {period.effective_from!r}") from error
        if previous_date is not None and effective_date <= previous_date:
            raise ValueError("tariff periods must be in ascending effective_from order")
        previous_date = effective_date
        validate_tariff_rules(f"period {period.effective_from} import", period.import_)
        validate_tariff_rules(f"period {period.effective_from} export", period.export)

    endpoint = urlparse(config.forecast.endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("forecast.endpoint must be an HTTP or HTTPS URL")

    panel_ids = [array.panel_id for array in config.forecast.arrays]
    supported_panel_ids = {1,2,3,4}
    invalid_panel_ids = set(panel_ids) - supported_panel_ids
    if invalid_panel_ids:
        invalid = ", ".join(str(panel_id) for panel_id in sorted(invalid_panel_ids))
        raise ValueError(f"unsupported forecast panel IDs: {invalid}")

    if len(panel_ids) != len(set(panel_ids)):
        raise ValueError("forecast panels must be unique")

    for array in config.forecast.arrays:
        if not -90 <= array.latitude <= 90:
            raise ValueError(f"forecast panel {array.panel_id} latitude must be between -90 and 90")

        if not -180 <= array.longitude <= 180:
            raise ValueError(
                f"forecast panel {array.panel_id} longitude must be between -180 and 180"
            )

        if not 0 <= array.declination <= 90:
            raise ValueError(
                f"forecast panel {array.panel_id} declination must be between 0 and 90"
            )

        if not -180 <= array.azimuth <= 180:
            raise ValueError(
                f"forecast panel {array.panel_id} azimuth must be between -180 and 180"
            )

    invalid_pv_strings = set(config.dashboard.actuals_to_forecast) - {"pv1", "pv2", "pv3", "pv4"}
    if invalid_pv_strings:
        raise ValueError(f"unsupported PV strings: {', '.join(sorted(invalid_pv_strings))}")

    unknown_panel_ids = set(config.dashboard.actuals_to_forecast.values()) - set(panel_ids)
    if unknown_panel_ids:
        unknown = ", ".join(str(panel_id) for panel_id in sorted(unknown_panel_ids))
        raise ValueError(f"dashboard mappings use unknown panel IDs: {unknown}")

    if not 1 <= config.actuals.modbus.port <= 65535:
        raise ValueError("Modbus port must be between 1 and 65535")

    if config.actuals.modbus.timeout_seconds <= 0:
        raise ValueError("Modbus timeout must be greater than zero")

    if not 1 <= config.actuals.modbus.default_device_id <= 247:
        raise ValueError("Modbus default device ID must be between 1 and 247")

    if not 1 <= config.dashboard.port <= 65535:
        raise ValueError("Dashboard port must be between 1 and 65535")

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def tariff_time_minutes(value: str, *, allow_end_of_day: bool = False) -> int:
    if allow_end_of_day and value == "24:00":
        return 24 * 60
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        maximum = "24:00" if allow_end_of_day else "23:59"
        raise ValueError(f"tariff time must use HH:MM from 00:00 to {maximum}: {value!r}")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def tariff_rule_matches(
    rule: TariffRule,
    weekday_index: int,
    minute_of_day: int,
) -> bool:
    start_minute = tariff_time_minutes(rule.start_time)
    end_minute = tariff_time_minutes(rule.end_time, allow_end_of_day=True)
    weekday = WEEKDAYS[weekday_index]
    if start_minute == end_minute:
        return weekday in rule.days
    if start_minute < end_minute:
        return weekday in rule.days and start_minute <= minute_of_day < end_minute
    previous_weekday = WEEKDAYS[(weekday_index - 1) % len(WEEKDAYS)]
    return (
        weekday in rule.days and minute_of_day >= start_minute
    ) or (
        previous_weekday in rule.days and minute_of_day < end_minute
    )


def validate_tariff_rules(direction: str, rules: tuple[TariffRule, ...]) -> None:
    if not rules:
        raise ValueError(f"tariffs.{direction} must contain at least one rule")

    for rule in rules:
        if not rule.days:
            raise ValueError(f"tariffs.{direction} rules must specify one or more days")

        invalid_days = set(rule.days) - set(WEEKDAYS)
        if invalid_days:
            raise ValueError(
                f"tariffs.{direction} uses invalid days: {', '.join(sorted(invalid_days))}"
            )

        if len(rule.days) != len(set(rule.days)):
            raise ValueError(f"tariffs.{direction} rules must not repeat days")

        tariff_time_minutes(rule.start_time)

        tariff_time_minutes(rule.end_time, allow_end_of_day=True)

        if not math.isfinite(rule.rate) or rule.rate < 0:
            raise ValueError(f"tariffs.{direction} rates must be finite and non-negative")

        if direction.endswith(" import") and (not rule.name or not rule.name.strip()):
            raise ValueError(f"tariffs.{direction} import rules must specify a tariff band name")

    for weekday_index, day in enumerate(WEEKDAYS):
        for minute_of_day in range(24 * 60):
            matching_rules = [
                rule
                for rule in rules
                if tariff_rule_matches(rule, weekday_index, minute_of_day)
            ]
            if len(matching_rules) != 1:
                raise ValueError(
                    f"tariffs.{direction} must have exactly one rule for "
                    f"{day} {minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
                )
