from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


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
class ModbusConfig:
    host: str
    port: int
    timeout_seconds: float
    default_device_id: int
    register_map: str


@dataclass(frozen=True)
class Config:
    timezone: str
    logging: LoggingConfig
    database: DatabaseConfig
    scheduler: SchedulerConfig
    forecast: ForecastConfig
    modbus: ModbusConfig


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as file:
        values = yaml.safe_load(file)
    if not isinstance(values, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    try:
        arrays = tuple(SolarArrayConfig(**array) for array in values["forecast"]["arrays"])
        config = Config(
            timezone=str(values["timezone"]),
            logging=LoggingConfig(**values["logging"]),
            database=DatabaseConfig(**values["database"]),
            scheduler=SchedulerConfig(**values["scheduler"]),
            forecast=ForecastConfig(
                interval_seconds=int(values["forecast"]["interval_seconds"]),
                timeout_seconds=float(values["forecast"]["timeout_seconds"]),
                endpoint=str(values["forecast"]["endpoint"]),
                arrays=arrays,
            ),
            modbus=ModbusConfig(
                host=str(values["modbus"]["host"]),
                port=int(values["modbus"]["port"]),
                timeout_seconds=float(values["modbus"]["timeout_seconds"]),
                default_device_id=int(values["modbus"]["default_device_id"]),
                register_map=str(values["modbus"]["register_map"]),
            ),
        )
        if config.scheduler.interval_seconds <= 0 or config.forecast.interval_seconds <= 0:
            raise ValueError("scheduler and forecast intervals must be greater than zero")
        if config.forecast.timeout_seconds <= 0 or not config.forecast.arrays:
            raise ValueError("forecast timeout must be greater than zero and arrays cannot be empty")
        if not 1 <= config.modbus.port <= 65535:
            raise ValueError("Modbus port must be between 1 and 65535")
        return config
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error
