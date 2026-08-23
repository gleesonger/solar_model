from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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


def require_mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{location} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{location} field names must be strings")
    return cast(dict[str, Any], value)


def require_fields(values: dict[str, Any], expected: set[str], location: str) -> None:
    missing = expected - set(values)
    unexpected = set(values) - expected
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(
            f"{location} contains unexpected fields: {', '.join(sorted(unexpected))}"
        )


def require_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{location} must be a non-empty string")
    return value.strip()


def require_int(value: object, location: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{location} must be an integer")
    return cast(int, value)


def require_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{location} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{location} must be finite")
    return number


def load_scheduler(value: object, location: str) -> SchedulerConfig:
    values = require_mapping(value, location)
    require_fields(values, {"interval_seconds"}, location)
    return SchedulerConfig(
        interval_seconds=require_int(
            values["interval_seconds"], f"{location}.interval_seconds"
        )
    )


def load_solar_array(value: object, index: int) -> SolarArrayConfig:
    location = f"forecast.arrays[{index}]"
    values = require_mapping(value, location)
    require_fields(
        values,
        {"panel_id", "name", "latitude", "longitude", "declination", "azimuth", "peak_kw"},
        location,
    )
    return SolarArrayConfig(
        panel_id=require_int(values["panel_id"], f"{location}.panel_id"),
        name=require_text(values["name"], f"{location}.name"),
        latitude=require_number(values["latitude"], f"{location}.latitude"),
        longitude=require_number(values["longitude"], f"{location}.longitude"),
        declination=require_number(values["declination"], f"{location}.declination"),
        azimuth=require_number(values["azimuth"], f"{location}.azimuth"),
        peak_kw=require_number(values["peak_kw"], f"{location}.peak_kw"),
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as file:
        loaded = yaml.safe_load(file)

    try:
        values = require_mapping(loaded, "configuration")
        require_fields(
            values,
            {"timezone", "logging", "database", "dashboard", "live", "forecast", "actuals"},
            "configuration",
        )

        logging_values = require_mapping(values["logging"], "logging")
        require_fields(logging_values, {"level"}, "logging")
        database_values = require_mapping(values["database"], "database")
        require_fields(database_values, {"path"}, "database")
        dashboard_values = require_mapping(values["dashboard"], "dashboard")
        require_fields(dashboard_values, {"port", "actuals_to_forecast"}, "dashboard")
        live_values = require_mapping(values["live"], "live")
        require_fields(live_values, {"scheduler"}, "live")
        forecast_values = require_mapping(values["forecast"], "forecast")
        require_fields(
            forecast_values,
            {"interval_seconds", "timeout_seconds", "endpoint", "arrays"},
            "forecast",
        )
        actuals_values = require_mapping(values["actuals"], "actuals")
        require_fields(actuals_values, {"scheduler", "modbus"}, "actuals")
        modbus_values = require_mapping(actuals_values["modbus"], "actuals.modbus")
        require_fields(
            modbus_values,
            {
                "host",
                "port",
                "timeout_seconds",
                "default_device_id",
                "register_map",
                "device_register_map",
            },
            "actuals.modbus",
        )

        raw_arrays = forecast_values["arrays"]
        if not isinstance(raw_arrays, list) or not raw_arrays:
            raise TypeError("forecast.arrays must be a non-empty list")
        arrays = tuple(
            load_solar_array(raw_array, index)
            for index, raw_array in enumerate(raw_arrays)
        )
        raw_actuals_to_forecast = require_mapping(
            dashboard_values["actuals_to_forecast"], "dashboard.actuals_to_forecast"
        )
        actuals_to_forecast = {
            require_text(actual, "dashboard.actuals_to_forecast key").casefold(): require_int(
                panel_id, f"dashboard.actuals_to_forecast.{actual}"
            )
            for actual, panel_id in raw_actuals_to_forecast.items()
        }
        config = Config(
            timezone=require_text(values["timezone"], "timezone"),
            logging=LoggingConfig(
                level=require_text(logging_values["level"], "logging.level").upper()
            ),
            database=DatabaseConfig(
                path=require_text(database_values["path"], "database.path")
            ),
            forecast=ForecastConfig(
                interval_seconds=require_int(
                    forecast_values["interval_seconds"], "forecast.interval_seconds"
                ),
                timeout_seconds=require_number(
                    forecast_values["timeout_seconds"], "forecast.timeout_seconds"
                ),
                endpoint=require_text(forecast_values["endpoint"], "forecast.endpoint"),
                arrays=arrays,
            ),
            dashboard=DashboardConfig(
                port=require_int(dashboard_values["port"], "dashboard.port"),
                actuals_to_forecast=actuals_to_forecast,
            ),
            actuals=ActualsConfig(
                scheduler=load_scheduler(actuals_values["scheduler"], "actuals.scheduler"),
                modbus=ModbusConfig(
                    host=require_text(modbus_values["host"], "actuals.modbus.host"),
                    port=require_int(modbus_values["port"], "actuals.modbus.port"),
                    timeout_seconds=require_number(
                        modbus_values["timeout_seconds"], "actuals.modbus.timeout_seconds"
                    ),
                    default_device_id=require_int(
                        modbus_values["default_device_id"],
                        "actuals.modbus.default_device_id",
                    ),
                    register_map=source_path(
                        require_text(modbus_values["register_map"], "actuals.modbus.register_map")
                    ),
                    device_register_map=source_path(
                        require_text(
                            modbus_values["device_register_map"],
                            "actuals.modbus.device_register_map",
                        )
                    ),
                ),
            ),
            live=LiveConfig(scheduler=load_scheduler(live_values["scheduler"], "live.scheduler")),
        )
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
        invalid_panel_ids = set(panel_ids) - {1, 2, 3, 4}
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
            if array.peak_kw <= 0:
                raise ValueError(f"forecast panel {array.panel_id} peak_kw must be greater than zero")
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
        return config
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error


def source_path(value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else Path(__file__).resolve().parent / path
