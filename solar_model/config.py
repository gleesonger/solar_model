from __future__ import annotations

import logging
import math
import re
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import date
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml
import dacite
from solar_model.planning.markov_transitions import validate_transition_inputs

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
class ForecastModelConfig:
    lookback: float | None
    halflife: float
    # Decimal influence after one unit of weighted/effective evidence (0.2 = 20%).
    prior_effective_observations: float | None

    def __post_init__(self) -> None:
        if self.lookback is not None and (not math.isfinite(self.lookback) or self.lookback <= 0):
            raise ValueError("forecast model lookback must be finite and greater than zero, or null")
        if not math.isfinite(self.halflife) or self.halflife <= 0:
            raise ValueError("forecast model halflife must be finite and greater than zero")
        fraction = self.prior_effective_observations
        if fraction is not None and (not math.isfinite(fraction) or not 0 < fraction <= 1):
            raise ValueError("prior_effective_observations must be a finite decimal fraction greater than 0 and at most 1, or null")

    @property
    def prior_strength(self) -> float:
        if self.prior_effective_observations is None:
            raise ValueError("this forecast model has no prior influence")
        return 1.0 / self.prior_effective_observations - 1.0


@dataclass(frozen=True)
class ForecastModelsConfig:
    solar_day_ahead: ForecastModelConfig = field(default_factory=lambda: ForecastModelConfig(56., 21., None))
    solar_intraday: ForecastModelConfig = field(default_factory=lambda: ForecastModelConfig(None, 1., 1.0 / 3.0))
    load: ForecastModelConfig = field(default_factory=lambda: ForecastModelConfig(42., 14., 0.2))

    def __post_init__(self) -> None:
        if self.solar_day_ahead.lookback is None or self.solar_day_ahead.prior_effective_observations is not None:
            raise ValueError("forecast.solar.model_calibration_parameters.day_ahead requires lookback and a null prior_effective_observations")
        if self.load.lookback is None or self.load.prior_effective_observations is None:
            raise ValueError("forecast.load.model_calibration_parameters requires lookback and prior_effective_observations")
        if self.solar_intraday.prior_effective_observations is None:
            raise ValueError("forecast.solar.model_calibration_parameters.intraday requires prior_effective_observations")


@dataclass(frozen=True)
class SolarForecastModelsConfig:
    day_ahead: ForecastModelConfig
    intraday: ForecastModelConfig


@dataclass(frozen=True)
class SolarForecastConfig:
    interval_seconds: int
    timeout_seconds: float
    endpoint: str
    arrays: tuple[SolarArrayConfig, ...]
    model_calibration_parameters: SolarForecastModelsConfig


@dataclass(frozen=True)
class LoadForecastConfig:
    model_calibration_parameters: ForecastModelConfig


@dataclass(frozen=True)
class ForecastConfig:
    solar: SolarForecastConfig
    load: LoadForecastConfig

    @property
    def models(self) -> ForecastModelsConfig:
        """Supply the model bundle shared by forecasting calculations and services."""
        return ForecastModelsConfig(self.solar.model_calibration_parameters.day_ahead,
                                    self.solar.model_calibration_parameters.intraday,
                                    self.load.model_calibration_parameters)


@dataclass(frozen=True)
class ScenarioDimensionConfig:
    low: float
    med: float
    high: float

    def items(self) -> tuple[tuple[str, float], ...]:
        return (("low", self.low), ("med", self.med), ("high", self.high))

    def __post_init__(self) -> None:
        for _, value in self.items():
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("scenario_multiplier values must be finite and non-negative")
        if not self.low <= self.med <= self.high:
            raise ValueError("scenario_multiplier values must be in low, med, high order")


@dataclass(frozen=True)
class ScenarioMultiplierConfig:
    solar: ScenarioDimensionConfig
    house_load: ScenarioDimensionConfig


@dataclass(frozen=True)
class TransitionMatrixConfig:
    solar: tuple[tuple[float, ...], ...]
    house_load: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class OptimiserConfig:
    compile_model_to_native_code: bool
    transition_hours: float
    transition_matrix: TransitionMatrixConfig
    projection_resolution_mins: tuple[int, ...]
    grid_step_percent: float
    power_divisions: int


@dataclass(frozen=True)
class PlanHistoryConfig:
    retain_snapshots_frequency_mins: int
    retain_snapshots_period_days: int


@dataclass(frozen=True)
class EnergyPlanConfig:
    num_projection_hours: int
    scenario_multiplier: ScenarioMultiplierConfig
    plan_rerun_frequency_mins: float
    num_visible_projection_hours: int
    calculation_timeout_seconds: float
    history: PlanHistoryConfig
    protection_preference: float
    minimum_battery_kwh: tuple[TariffRule, ...]
    maximum_battery_kwh: tuple[TariffRule, ...]
    limit_battery_export_rate_to_grid_kw: tuple[TariffRule, ...]
    preferred_reserve_kwh: float
    min_forced_charge_power_kw: float
    min_forced_discharge_power_kw: float
    max_forced_charge_power_kw: float
    max_forced_discharge_power_kw: float
    optimiser: OptimiserConfig


@dataclass(frozen=True)
class AdditionalDeviceInfoConfig:
    inverter_ac_to_dc_efficiency: float
    inverter_dc_to_ac_efficiency: float
    battery_charge_efficiency: float
    battery_discharge_efficiency: float


@dataclass(frozen=True)
class DashboardConfig:
    port: int
    host: str
    actuals_to_forecast: dict[str, str]


@dataclass(frozen=True)
class TariffRule:
    days: tuple[str, ...]
    start_time: str
    end_time: str
    rate: float
    name: str | None

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
    additional_device_info: AdditionalDeviceInfoConfig
    dashboard: DashboardConfig
    tariffs: TariffsConfig
    actuals: ActualsConfig

    @property
    def actuals_to_forecast_panel_ids(self) -> dict[str, int]:
        """Resolve configured array names for ID-based storage and chart columns."""
        panel_ids = {array.name: array.panel_id for array in self.forecast.solar.arrays}
        return {pv: panel_ids[name] for pv, name in self.dashboard.actuals_to_forecast.items()}


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as file:
        raw_config = yaml.safe_load(file)

    return config_from_mapping(raw_config, str(path))


def require_config_values(schema: type[Any], values: Any, path: str = "") -> None:
    """Require explicit values even for nullable fields and nested list entries."""
    if not isinstance(values, dict):
        raise ValueError(f"{path or 'configuration'} must be a mapping")
    hints = get_type_hints(schema)
    for definition in fields(schema):
        name = definition.name
        label = "import" if name == "import_" else name
        location = f"{path}.{label}" if path else label
        if name not in values:
            raise ValueError(f"missing required configuration value: {location}")
        annotation = hints[name]
        if is_dataclass(annotation):
            require_config_values(annotation, values[name], location)
        elif get_origin(annotation) in (tuple, list) and isinstance(values[name], (tuple, list)):
            item_type = get_args(annotation)[0]
            if is_dataclass(item_type):
                for index, item in enumerate(values[name]):
                    require_config_values(item_type, item, f"{location}[{index}]")


def config_from_mapping(raw_config: dict[str, Any], source: str = "configuration", *, strict: bool = False) -> Config:
    """Normalise and validate a complete configuration mapping."""
    try:
        if isinstance(raw_config, dict) and "scenarios" in raw_config:
            raise ValueError("replace scenarios with energy_plan.scenario_multiplier")
        energy_plan_data = raw_config.get("energy_plan") if isinstance(raw_config, dict) else None
        if isinstance(energy_plan_data, dict):
            if "scenarios" in energy_plan_data:
                raise ValueError("rename energy_plan.scenarios to energy_plan.scenario_multiplier and use numeric low/med/high multipliers without probabilities")
            if "optimiser_specific" in energy_plan_data:
                raise ValueError("rename energy_plan.optimiser_specific to energy_plan.optimiser")
            if "projection_resolution_mins" in energy_plan_data or "dynamic_programming" in energy_plan_data:
                raise ValueError("move optimiser settings under the flat energy_plan.optimiser block")
            optimiser_data = energy_plan_data.get("optimiser", {})
            if isinstance(optimiser_data, dict):
                retired = set(optimiser_data) - {definition.name for definition in fields(OptimiserConfig)} - {"comment"}
                if retired:
                    raise ValueError("energy_plan.optimiser is Markov-only; remove legacy selectors/blocks and explicitly supply the flat optimiser settings: " + ", ".join(sorted(retired)))
                resolutions = optimiser_data.get("projection_resolution_mins")
                if resolutions is not None and not isinstance(resolutions, list):
                    raise ValueError("optimiser.projection_resolution_mins must be an array")
                if isinstance(resolutions, list):
                    expanded = []
                    for entry in resolutions:
                        if isinstance(entry, str):
                            minutes_text, separator, count_text = entry.partition("*")
                            minutes, count = int(minutes_text), int(count_text) if separator else 1
                            if minutes <= 0 or count <= 0:
                                raise ValueError("projection_resolution_mins repetitions must be positive")
                            expanded.extend([minutes] * count)
                        else:
                            expanded.append(entry)
                    optimiser_data["projection_resolution_mins"] = expanded
            backup_data = energy_plan_data.get("minimum_battery_kwh")
            if isinstance(backup_data, dict):
                # This is documentation for people editing config.yaml, not configuration.
                backup_data.pop("comment", None)
                if "rules" in backup_data:
                    energy_plan_data["minimum_battery_kwh"] = backup_data["rules"]
            for name in ("minimum_battery_kwh", "maximum_battery_kwh", "limit_battery_export_rate_to_grid_kw"):
                rules = energy_plan_data.get(name)
                if isinstance(rules, list):
                    for rule in rules:
                        if isinstance(rule, dict):
                            rule.pop("comment", None)

        tariff_data = raw_config.get("tariffs") if isinstance(raw_config, dict) else None
        if isinstance(tariff_data, dict) and isinstance(tariff_data.get("periods"), list):
            for period in tariff_data["periods"]:
                if isinstance(period, dict) and "import" in period:
                    period["import_"] = period.pop("import")
        require_config_values(Config, raw_config)
        scenario_data = energy_plan_data.get("scenario_multiplier") if isinstance(energy_plan_data, dict) else None
        if isinstance(scenario_data, dict):
            if set(scenario_data) != {"solar", "house_load"}:
                raise ValueError("energy_plan.scenario_multiplier requires exactly solar and house_load")
            for name in ("solar", "house_load"):
                levels = scenario_data[name]
                if not isinstance(levels, dict) or set(levels) != {"low", "med", "high"}:
                    raise ValueError(f"energy_plan.scenario_multiplier.{name} requires exactly low, med, high entries")
        config = dacite.from_dict(
            data_class=Config,
            data=raw_config,
            config=dacite.Config(
                cast=[tuple],
                strict=strict,
            ),
        )
        validate_config(config)
        return config
    except (dacite.DaciteError, TypeError, ValueError) as error:
        raise ValueError(f"invalid configuration in {source}: {error}") from error


class EnergyPlanYamlDumper(yaml.SafeDumper):
    """Use the compact list/map layout of the hand-written configuration."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)

    def represent_sequence(self, tag: str, sequence: Sequence[Any], flow_style: bool | None = None) -> yaml.SequenceNode:
        inline = all(not isinstance(value, (dict, list, tuple)) for value in sequence)
        return super().represent_sequence(tag, sequence, flow_style=inline)

    def represent_mapping(self, tag: str, mapping: Mapping[Any, Any], flow_style: bool | None = None) -> yaml.MappingNode:
        inline = set(mapping) == {"low", "med", "high"}
        return super().represent_mapping(tag, mapping, flow_style=inline)

    def represent_scalar(self, tag: str, value: str, style: str | None = None) -> yaml.ScalarNode:
        if tag == "tag:yaml.org,2002:str" and re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            style = '"'
        return super().represent_scalar(tag, value, style)


def energy_plan_yaml(settings: EnergyPlanConfig) -> str:
    """Serialise the public YAML layout rather than dumping the dataclass layout."""
    sections = [{
        "num_projection_hours": settings.num_projection_hours,
        "num_visible_projection_hours": settings.num_visible_projection_hours,
        "plan_rerun_frequency_mins": settings.plan_rerun_frequency_mins,
        "calculation_timeout_seconds": settings.calculation_timeout_seconds,
        "preferred_reserve_kwh": settings.preferred_reserve_kwh,
        "protection_preference": settings.protection_preference,
    }]
    sections.append({"history": asdict(settings.history)})
    sections.append({"scenario_multiplier": {name: dict(dimension.items()) for name, dimension in (("solar", settings.scenario_multiplier.solar), ("house_load", settings.scenario_multiplier.house_load))}})
    sections.append({
        "min_forced_charge_power_kw": settings.min_forced_charge_power_kw,
        "min_forced_discharge_power_kw": settings.min_forced_discharge_power_kw,
        "max_forced_charge_power_kw": settings.max_forced_charge_power_kw,
        "max_forced_discharge_power_kw": settings.max_forced_discharge_power_kw,
    })
    optimiser = asdict(settings.optimiser)
    optimiser["projection_resolution_mins"] = collapse_projection_resolutions(settings.optimiser.projection_resolution_mins)
    optimiser["transition_matrix"] = {name: [list(row) for row in getattr(settings.optimiser.transition_matrix, name)] for name in ("solar", "house_load")}
    sections.append({"optimiser": optimiser})
    for name in ("minimum_battery_kwh", "maximum_battery_kwh", "limit_battery_export_rate_to_grid_kw"):
        sections.append({name: collapse_scheduled_rules(getattr(settings, name))})
    return "\n".join(yaml.dump(section, Dumper=EnergyPlanYamlDumper, sort_keys=False, allow_unicode=True, width=1000) for section in sections)


def collapse_projection_resolutions(resolutions: tuple[int, ...]) -> list[int | str]:
    """Encode consecutive equal timesteps as minutes*count, preserving order."""
    runs = []
    for minutes in resolutions:
        if runs and runs[-1][0] == minutes:
            runs[-1][1] += 1
        else:
            runs.append([minutes, 1])
    return [f"{minutes}*{count}" if count > 1 else minutes for minutes, count in runs]


def collapse_scheduled_rules(rules: tuple[TariffRule, ...]) -> list[dict[str, Any]]:
    """Combine matching schedules across weekdays without changing time windows."""
    grouped: dict[tuple[str, str, float, str | None], set[str]] = {}
    for rule in rules:
        key = (rule.start_time, rule.end_time, rule.rate, rule.name)
        if key not in grouped:
            grouped[key] = set()
        grouped[key].update(rule.days)
    result: list[dict[str, Any]] = []
    for (start, end, rate, name), days in grouped.items():
        entry: dict[str, Any] = {"days": [day for day in WEEKDAYS if day in days]}
        entry["name"] = name
        entry.update(start_time=start, end_time=end, rate=rate)
        result.append(entry)
    return result


def parse_energy_plan_yaml(config: Config, text: str) -> EnergyPlanConfig:
    """Parse a full UI energy_plan replacement without mutating the global config."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid energy_plan YAML: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("Energy plan settings must be a YAML mapping")
    raw = yaml.safe_load(yaml.safe_dump(asdict(config)))
    raw["energy_plan"] = data
    return config_from_mapping(raw, "energy_plan editor", strict=True).energy_plan


def save_energy_plan_yaml(path: str | Path, config: Config, text: str) -> None:
    """Atomically replace only energy_plan; preserve all text outside that section."""
    parse_energy_plan_yaml(config, text)
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as file:
        original = file.read()
    try:
        root = yaml.compose(original)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid configuration file YAML: {error}") from error
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("Configuration file must be a YAML mapping")
    section = next(((key, value) for key, value in root.value if key.value == "energy_plan"), None)
    if section is None:
        raise ValueError("Configuration file has no energy_plan section")
    key, value = section
    end = value.end_mark.index
    # Leave trailing blank lines and comments outside the regenerated section intact.
    section_lines = original[key.start_mark.index:end].splitlines(keepends=True)
    while section_lines and (not section_lines[-1].strip() or section_lines[-1].lstrip().startswith("#")):
        end -= len(section_lines.pop())
    replacement = "energy_plan:\n" + "".join("  " + line + "\n" for line in text.rstrip().splitlines())
    updated = original[:key.start_mark.index] + replacement + original[end:]
    # Also validate against the latest on-disk configuration, before writing anything.
    config_from_mapping(yaml.safe_load(updated), str(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(updated)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_config(config: Config) -> None:
    """Validate configuration values after the YAML has been deserialized."""
    if (
        config.actuals.data_retrival_schedule.live_update_interval_seconds <= 0
        or config.actuals.data_retrival_schedule.full_updated_interval_seconds <= 0
        or config.forecast.solar.interval_seconds <= 0
    ):
        raise ValueError("scheduler, live, and forecast intervals must be greater than zero")

    if config.forecast.solar.timeout_seconds <= 0:
        raise ValueError("forecast timeout must be greater than zero")

    config.forecast.models  # Validate model-specific requirements across both sections.
    energy_plan = config.energy_plan
    if not math.isfinite(energy_plan.plan_rerun_frequency_mins) or energy_plan.plan_rerun_frequency_mins <= 0:
        raise ValueError("energy_plan.plan_rerun_frequency_mins must be finite and greater than zero")
    if isinstance(energy_plan.calculation_timeout_seconds, bool) or not math.isfinite(energy_plan.calculation_timeout_seconds) or energy_plan.calculation_timeout_seconds <= 0:
        raise ValueError("energy_plan.calculation_timeout_seconds must be finite and greater than zero")
    for name in ("retain_snapshots_frequency_mins", "retain_snapshots_period_days"):
        value = getattr(energy_plan.history, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"energy_plan.history.{name} must be a positive integer")
    dp = energy_plan.optimiser
    if type(dp.compile_model_to_native_code) is not bool:
        raise ValueError("optimiser.compile_model_to_native_code must be true or false")
    for name in ("solar", "house_load"):
        validate_transition_inputs(getattr(dp.transition_matrix, name), dp.transition_hours, matrix_name=f"optimiser.transition_matrix.{name}")
    if energy_plan.scenario_multiplier.solar.med <= 0 or energy_plan.scenario_multiplier.house_load.med <= 0:
        raise ValueError("Markov DP requires positive medium multipliers")
    if not math.isfinite(dp.grid_step_percent) or not 0 < dp.grid_step_percent <= 100 or type(dp.grid_step_percent) is bool:
        raise ValueError("optimiser.grid_step_percent must be in (0,100]")
    if type(dp.power_divisions) is not int or dp.power_divisions <= 0:
        raise ValueError("optimiser.power_divisions must be a positive integer")
    for name in (
        "min_forced_charge_power_kw",
        "min_forced_discharge_power_kw",
        "max_forced_charge_power_kw",
        "max_forced_discharge_power_kw",
    ):
        value = getattr(energy_plan, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"energy_plan.{name} must be finite and greater than zero")
    if not math.isfinite(energy_plan.preferred_reserve_kwh) or energy_plan.preferred_reserve_kwh <= 0:
        raise ValueError("preferred_reserve_kwh must be finite and greater than zero")
    if type(energy_plan.num_projection_hours) is not int or energy_plan.num_projection_hours <= 0:
        raise ValueError("num_projection_hours must be a positive integer")
    if type(energy_plan.num_visible_projection_hours) is not int or not 0 < energy_plan.num_visible_projection_hours <= energy_plan.num_projection_hours:
        raise ValueError("num_visible_projection_hours must be positive and no greater than num_projection_hours")
    resolutions = dp.projection_resolution_mins
    if not resolutions or any(type(minutes) is not int or minutes <= 0 for minutes in resolutions):
        raise ValueError("optimiser.projection_resolution_mins must contain positive integer minutes")
    if sum(resolutions) < energy_plan.num_projection_hours * 60:
        raise ValueError("optimiser.projection_resolution_mins must cover num_projection_hours")
    if not math.isfinite(energy_plan.protection_preference) or energy_plan.protection_preference < 0:
        raise ValueError("energy_plan.protection_preference must be finite and non-negative")
    additional_device_info = config.additional_device_info
    for name in ("inverter_ac_to_dc_efficiency", "inverter_dc_to_ac_efficiency", "battery_charge_efficiency", "battery_discharge_efficiency"):
        efficiency = getattr(additional_device_info, name)
        if not math.isfinite(efficiency) or not 0 < efficiency <= 1:
            raise ValueError(f"additional_device_info.{name} must be finite, greater than zero and at most one")
    for name in ("minimum_battery_kwh", "maximum_battery_kwh", "limit_battery_export_rate_to_grid_kw"):
        validate_tariff_rules(f"energy_plan.{name}", getattr(energy_plan, name), require_complete=False)

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

    endpoint = urlparse(config.forecast.solar.endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("forecast.solar.endpoint must be an HTTP or HTTPS URL")

    panel_ids = [array.panel_id for array in config.forecast.solar.arrays]
    supported_panel_ids = {1,2,3,4}
    invalid_panel_ids = set(panel_ids) - supported_panel_ids
    if invalid_panel_ids:
        invalid = ", ".join(str(panel_id) for panel_id in sorted(invalid_panel_ids))
        raise ValueError(f"unsupported forecast panel IDs: {invalid}")

    if len(panel_ids) != len(set(panel_ids)):
        raise ValueError("forecast panels must be unique")

    for array in config.forecast.solar.arrays:
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

    array_names = [array.name for array in config.forecast.solar.arrays]
    if len(array_names) != len(set(array_names)):
        raise ValueError("forecast array names must be unique")
    unknown_names = set(config.dashboard.actuals_to_forecast.values()) - set(array_names)
    if unknown_names:
        raise ValueError(f"dashboard mappings use unknown array names: {', '.join(sorted(unknown_names))}")

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


def validate_tariff_rules(direction: str, rules: tuple[TariffRule, ...], *, require_complete: bool = True) -> None:
    if not rules and require_complete:
        raise ValueError(f"tariffs.{direction} must contain at least one rule")
    if not rules:
        return

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
            if len(matching_rules) > 1 or (require_complete and not matching_rules):
                raise ValueError(
                    f"tariffs.{direction} must have exactly one rule for "
                    f"{day} {minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
                )
