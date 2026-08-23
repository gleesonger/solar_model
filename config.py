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
    panel: str
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
    actuals_to_forecast: dict[str, str]


@dataclass(frozen=True)
class ModbusConfig:
    host: str
    port: int
    timeout_seconds: float
    default_device_id: int
    register_map: str
    device_register_map: str


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
        values = yaml.safe_load(file)
    if not isinstance(values, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    try:
        arrays = tuple(SolarArrayConfig(**array) for array in values["forecast"]["arrays"])
        dashboard_values = values.get("dashboard", {})
        if not isinstance(dashboard_values, dict):
            raise TypeError("dashboard must be a mapping")
        raw_actuals_to_forecast = dashboard_values.get("actuals_to_forecast", {})
        if not isinstance(raw_actuals_to_forecast, dict):
            raise TypeError("dashboard.actuals_to_forecast must be a mapping")
        actuals_to_forecast = {
            str(actual).casefold(): str(panel_id).casefold()
            for actual, panel_id in raw_actuals_to_forecast.items()
        }
        live_values = values.get("live", {"scheduler": {"interval_seconds": 5}})
        if not isinstance(live_values, dict):
            raise TypeError("live must be a mapping")
        config = Config(
            timezone=str(values["timezone"]),
            logging=LoggingConfig(**values["logging"]),
            database=DatabaseConfig(**values["database"]),
            forecast=ForecastConfig(
                interval_seconds=int(values["forecast"]["interval_seconds"]),
                timeout_seconds=float(values["forecast"]["timeout_seconds"]),
                endpoint=str(values["forecast"]["endpoint"]),
                arrays=arrays,
            ),
            dashboard=DashboardConfig(
                actuals_to_forecast=actuals_to_forecast,
            ),
            actuals=ActualsConfig(
                scheduler=SchedulerConfig(**values["actuals"]["scheduler"]),
                modbus=ModbusConfig(
                    host=str(values["actuals"]["modbus"]["host"]),
                    port=int(values["actuals"]["modbus"]["port"]),
                    timeout_seconds=float(values["actuals"]["modbus"]["timeout_seconds"]),
                    default_device_id=int(values["actuals"]["modbus"]["default_device_id"]),
                    register_map=str(values["actuals"]["modbus"]["register_map"]),
                    device_register_map=str(
                        values["actuals"]["modbus"].get(
                            "device_register_map", "sigen_device_register_map.json"
                        )
                    ),
                ),
            ),
            live=LiveConfig(
                scheduler=SchedulerConfig(**live_values["scheduler"]),
            ),
        )
        if (
            config.actuals.scheduler.interval_seconds <= 0
            or config.live.scheduler.interval_seconds <= 0
            or config.forecast.interval_seconds <= 0
        ):
            raise ValueError("scheduler, live, and forecast intervals must be greater than zero")
        if config.forecast.timeout_seconds <= 0 or not config.forecast.arrays:
            raise ValueError("forecast timeout must be greater than zero and arrays cannot be empty")
        panel_ids = [array.panel for array in config.forecast.arrays]
        invalid_panel_ids = set(panel_ids) - {"panel_1", "panel_2", "panel_3", "panel_4"}
        if invalid_panel_ids:
            raise ValueError(f"unsupported forecast panels: {', '.join(sorted(invalid_panel_ids))}")
        if len(panel_ids) != len(set(panel_ids)):
            raise ValueError("forecast panels must be unique")
        invalid_pv_strings = set(config.dashboard.actuals_to_forecast) - {"pv1", "pv2", "pv3", "pv4"}
        if invalid_pv_strings:
            raise ValueError(f"unsupported PV strings: {', '.join(sorted(invalid_pv_strings))}")
        unknown_panel_ids = set(config.dashboard.actuals_to_forecast.values()) - set(panel_ids)
        if unknown_panel_ids:
            raise ValueError(f"dashboard mappings use unknown panels: {', '.join(sorted(unknown_panel_ids))}")
        if not 1 <= config.actuals.modbus.port <= 65535:
            raise ValueError("Modbus port must be between 1 and 65535")
        return config
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error
