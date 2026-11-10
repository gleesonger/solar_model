"""Prepare reports in the worker and restore published plans from SQLite."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from solar_model.config import Config, EnergyPlanConfig, energy_plan_yaml, parse_energy_plan_yaml
from solar_model.planning.plan_report import advanced_plan_frame, detail_columns, detail_frame, hourly_detail_frame, scenario_summary
from solar_model.planning.types import NextAction, OperatingMode, SimulationResult
from solar_model.services.planning import PlanReport, generate_plan_report
from solar_model.storage.plan_history import StoredPlan, load_plan, save_plan


@dataclass(frozen=True)
class SavedRun:
    snapshot_id: str
    report: PlanReport
    settings: EnergyPlanConfig
    began_at: datetime
    full_data_at: datetime
    completed_at: datetime


def frame_payload(frame: pd.DataFrame) -> dict[str, Any]:
    """Use JSON floats, explicit UTC timestamps and nulls; never pickle objects."""
    frame = frame.copy()
    timestamps = [name for name in ("starts_at", "ends_at", "timestamp") if name in frame]
    for name in timestamps:
        frame[name] = pd.to_datetime(frame[name], utc=True).map(lambda value: value.isoformat())
    return {"timestamps": timestamps, "columns": list(frame.columns), "data": frame.astype(object).where(pd.notna(frame), None).values.tolist()}


def payload_frame(payload: dict[str, Any]) -> pd.DataFrame:
    frame = pd.DataFrame(payload["data"], columns=payload["columns"])
    for name in payload["timestamps"]:
        frame[name] = pd.to_datetime(frame[name], format="ISO8601", utc=True)
    return frame


def prepare_details(report: PlanReport, settings: EnergyPlanConfig, timezone_name: str) -> dict[str, dict[str, pd.DataFrame]]:
    prepared: dict[str, dict[str, pd.DataFrame]] = {"Compact": {}, "Advanced": {}}
    for scenario, visible in report.simulation.results.items():
        compact = detail_frame(visible, report.battery_capacity_kwh)
        compact["losses_kwh"] += compact["solar_clipped_kwh"]
        compact = hourly_detail_frame(compact, timezone_name, report.simulation.next_action)
        compact["t"] = (compact.ends_at - compact.starts_at).dt.total_seconds() / 3600
        prepared["Compact"][scenario] = compact[[column["name"] for column in detail_columns(compact, True)]]
        hidden = report.simulation.lookahead_results.get(scenario)
        full = pd.concat([visible, hidden], ignore_index=True) if hidden is not None and not hidden.empty else visible
        prepared["Advanced"][scenario] = advanced_plan_frame(detail_frame(full, report.battery_capacity_kwh), timezone_name, report.simulation.next_action)
    return prepared


def persist_report(config: Config, report: PlanReport, began_at: datetime, completed_at: datetime) -> str:
    if report.full_data_at is None:
        raise ValueError("A shared plan requires a source-reading timestamp")
    details = prepare_details(report, config.energy_plan, config.timezone)
    action = {field.name: getattr(report.simulation.next_action, field.name) for field in fields(NextAction)}
    action["mode"] = report.simulation.next_action.mode.value
    metadata: dict[str, Any] = {
        "version": 1,
        "began_at": began_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "full_data_at": report.full_data_at.isoformat(),
        "timezone": config.timezone,
        "model": config.energy_plan.optimiser.model,
        "settings": energy_plan_yaml(config.energy_plan),
        "action": action,
        "battery_capacity_kwh": report.battery_capacity_kwh,
        "command_battery_capacity_kwh": report.command_battery_capacity_kwh,
        "preferred_reserve_kwh": report.preferred_reserve_kwh,
        "protection_preference": report.protection_preference,
        "actuals": frame_payload(report.actuals),
        "original_columns": {name: list(frame.columns) for name, frame in report.simulation.results.items()},
        "visible_counts": {name: len(frame) for name, frame in report.simulation.results.items()},
        "original_actions": {},
        "summaries": {name: scenario_summary(frame, config.energy_plan.preferred_reserve_kwh) for name, frame in report.simulation.results.items()},
        "initial_battery_kwh": {name: float(frame.battery_start_kwh.iloc[0]) for name, frame in report.simulation.results.items()},
        "action_minutes": (next(iter(report.simulation.results.values())).ends_at.iloc[0] - next(iter(report.simulation.results.values())).starts_at.iloc[0]).total_seconds() / 60,
        "action_solar_export": {name: float(frame.solar_to_grid_kwh.iloc[0]) if "solar_to_grid_kwh" in frame else 0. for name, frame in report.simulation.results.items()},
    }
    for name, visible in report.simulation.results.items():
        hidden = report.simulation.lookahead_results.get(name)
        full = pd.concat([visible, hidden], ignore_index=True) if hidden is not None and not hidden.empty else visible
        if "plan_action" in full:
            metadata["original_actions"][name] = full.plan_action.tolist()
    sections = {(view, scenario): frame_payload(frame) for view, scenarios in details.items() for scenario, frame in scenarios.items()}
    # Optional model expectations are separate from scenario stress illustrations.
    for name in ("expected_score", "expected_results", "expected_lookahead_results"):
        value = getattr(report.simulation, name, None)
        if isinstance(value, pd.DataFrame):
            sections[("Expected", name)] = frame_payload(value)
        elif value is not None:
            metadata[name] = value
    return save_plan(config.database.path, completed_at, metadata, sections)


def generate_saved_plan(config: Config, began_at: datetime) -> str:
    report = generate_plan_report(config, began_at)
    return persist_report(config, report, began_at, datetime.now(timezone.utc))


def read_saved_run(config: Config, snapshot_id: str | None = None, historical: bool = False) -> SavedRun | None:
    stored = load_plan(config.database.path, snapshot_id)
    if stored is None:
        return None
    return restore_run(config, stored, historical)


def restore_run(config: Config, stored: StoredPlan, historical: bool = False) -> SavedRun:
    """Restore the latest full report; historical tables can load sections directly."""
    metadata = stored.metadata
    settings = parse_energy_plan_yaml(config, metadata["settings"])
    action = {**metadata["action"], "mode": OperatingMode(metadata["action"]["mode"])}
    visible, hidden = {}, {}
    details: dict[str, dict[str, pd.DataFrame]] = {"Compact": {}, "Advanced": {}}
    for (view, scenario), payload in stored.sections.items():
        if view in details:
            details[view][scenario] = payload_frame(payload)
    for scenario, frame in details["Advanced"].items():
        raw = frame.reindex(columns=metadata["original_columns"][scenario])
        if scenario in metadata["original_actions"]:
            raw["plan_action"] = metadata["original_actions"][scenario]
        count = metadata["visible_counts"][scenario]
        visible[scenario], hidden[scenario] = raw.iloc[:count].copy(), raw.iloc[count:].reset_index(drop=True)
    full_resolution = bool(visible)
    if not visible:
        for scenario, compact in details["Compact"].items():
            raw = compact.copy()
            initial = metadata.get("initial_battery_kwh", {}).get(scenario, float("nan"))
            raw["battery_start_kwh"] = [initial, *raw.battery_end_kwh.iloc[:-1]]
            visible[scenario], hidden[scenario] = raw, raw.iloc[:0].copy()
    actuals = payload_frame(metadata["actuals"]) if metadata.get("actuals") else pd.DataFrame()
    if historical and metadata.get("actuals") is None:
        from zoneinfo import ZoneInfo
        from solar_model.storage.connection import SolarDatabase, create_engine_for_database
        from solar_model.storage.queries.planning import load_plan_actuals
        cutoff = datetime.fromisoformat(metadata["began_at"])
        day_start = cutoff.astimezone(ZoneInfo(metadata["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0)
        database = SolarDatabase(create_engine_for_database(config.database.path))
        actuals = load_plan_actuals(database, day_start, cutoff, metadata["battery_capacity_kwh"])
    simulation = SimulationResult(NextAction(**action), visible, hidden)
    for name in ("expected_score", "expected_results", "expected_lookahead_results"):
        if hasattr(simulation, name):
            value = metadata.get(name)
            if ("Expected", name) in stored.sections:
                value = payload_frame(stored.sections[("Expected", name)])
            object.__setattr__(simulation, name, value)
    report = PlanReport(
        simulation=simulation,
        actuals=actuals,
        battery_capacity_kwh=metadata["battery_capacity_kwh"],
        scenarios=settings.scenarios,
        preferred_reserve_kwh=metadata["preferred_reserve_kwh"],
        protection_preference=metadata["protection_preference"],
        command_battery_capacity_kwh=metadata["command_battery_capacity_kwh"],
        full_data_at=datetime.fromisoformat(metadata["full_data_at"]),
        details=details,
        summaries=metadata.get("summaries"),
        action_minutes=metadata.get("action_minutes"),
        action_solar_export=metadata.get("action_solar_export"),
        full_resolution=full_resolution,
    )
    return SavedRun(stored.snapshot_id, report, settings, datetime.fromisoformat(metadata["began_at"]), report.full_data_at, datetime.fromisoformat(metadata["completed_at"]))
