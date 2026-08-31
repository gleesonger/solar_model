from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml
import dacite

from common import source_path

@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class DatabaseConfig:
    path: str


@dataclass(frozen=True)
class SchedulerConfig:
    interval_seconds: int


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
class DashboardConfig:
    port: int
    host: str
    actuals_to_forecast: dict[str, int]


@dataclass(frozen=True)
class ModbusConfig:
    host: str
    port: int
    timeout_seconds: float
    default_device_id: int
    register_map: Path
    device_register_map: Path


@dataclass(frozen=True)
class ActualsConfig:
    scheduler: SchedulerConfig
    modbus: ModbusConfig


@dataclass(frozen=True)
class LiveConfig:
    scheduler: SchedulerConfig


@dataclass(frozen=True)
class Config:
    timezone: str
    logging: LoggingConfig
    database: DatabaseConfig
    forecast: ForecastConfig
    dashboard: DashboardConfig
    actuals: ActualsConfig
    live: LiveConfig



def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as file:
        raw_config = yaml.safe_load(file)

    try:
        config = dacite.from_dict(
            data_class=Config,
            data=raw_config,
            config=dacite.Config(cast=[tuple], type_hooks={Path: source_path}),
        )
        validate_config(config)
        return config
    except (dacite.DaciteError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error


def validate_config(config: Config) -> None:
    """Validate configuration values after the YAML has been deserialized."""
    if (
        config.actuals.scheduler.interval_seconds <= 0
        or config.live.scheduler.interval_seconds <= 0
        or config.forecast.interval_seconds <= 0
    ):
        raise ValueError("scheduler, live, and forecast intervals must be greater than zero")

    if config.forecast.timeout_seconds <= 0:
        raise ValueError("forecast timeout must be greater than zero")

    try:
        ZoneInfo(config.timezone)
    except KeyError as error:
        raise ValueError(f"timezone is not valid: {config.timezone}") from error

    if config.logging.level not in logging.getLevelNamesMapping():
        raise ValueError(f"logging.level is not valid: {config.logging.level}")

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

    for register_map in (
        config.actuals.modbus.register_map,
        config.actuals.modbus.device_register_map,
    ):
        if not register_map.is_file():
            raise ValueError(f"register map does not exist: {register_map}")
