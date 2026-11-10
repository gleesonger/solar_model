"""Dashboard cache/subscriptions for plans published by the independent service."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from yaml import YAMLError

from nicegui import run

from solar_model.config import Config, EnergyPlanConfig, load_config, parse_energy_plan_yaml, save_energy_plan_yaml
from solar_model.dashboard.planning_worker import demonstrate
from solar_model.logging_setup import LOGGER
from solar_model.planning.types import NoFeasiblePlanError
from solar_model.services.planning import PlanReport
from solar_model.services.plan_history import read_saved_run
from solar_model.storage.plan_history import latest_plan_id, read_planner_status


@dataclass(frozen=True)
class PlanRun:
    report: PlanReport
    settings: EnergyPlanConfig | str
    began_at: datetime
    full_data_at: datetime
    completed_at: datetime
    demonstration: bool = False
    snapshot_id: str | None = None


class PlanStore:
    """Keep one shared actual plan; user calculations remain private demonstrations."""

    def __init__(self, config: Config, config_path: str = "config.yaml") -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.current: PlanRun | None = None
        self.version = 0
        self.last_error: str | None = None
        self._service_running = False
        self._listeners: set[Callable[[], None]] = set()
        self._stop = asyncio.Event()
        self._disk_signature: tuple[int, int] | None = None

    @property
    def running(self) -> bool:
        return self._service_running

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _publish(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                LOGGER.exception("Plan subscriber failed")

    def _signature(self) -> tuple[int, int]:
        stat = self.config_path.stat()
        return stat.st_mtime_ns, stat.st_size

    async def reload_settings(self) -> None:
        """Hot-reload only energy_plan; other application settings retain startup semantics."""
        signature = self._signature()
        if signature != self._disk_signature:
            try:
                updated = await run.io_bound(load_config, self.config_path)
            except (ValueError, YAMLError) as error:
                self._disk_signature = signature
                LOGGER.warning("Unable to reload energy_plan; retaining the last valid settings: %s", error)
                return
            if signature == self._signature():
                self.config = replace(self.config, energy_plan=updated.energy_plan)
                self._disk_signature = signature

    def save_settings(self, text: str) -> None:
        settings = parse_energy_plan_yaml(self.config, text)
        save_energy_plan_yaml(self.config_path, self.config, text)
        self.config = replace(self.config, energy_plan=settings)
        self._disk_signature = None

    async def demonstrate(self, settings: EnergyPlanConfig) -> PlanRun:
        result = await demonstrate(replace(self.config, energy_plan=settings))
        report = result["report"]
        if report.full_data_at is None:
            raise RuntimeError("Demonstration did not return a source-reading timestamp")
        return PlanRun(report, settings, result["began_at"], report.full_data_at, result["completed_at"], demonstration=True)

    async def check_for_update(self) -> None:
        await self.reload_settings()
        snapshot_id = await run.io_bound(latest_plan_id, self.config.database.path)
        changed = False
        if snapshot_id is not None and (self.current is None or snapshot_id != self.current.snapshot_id):
            saved = await run.io_bound(read_saved_run, self.config, snapshot_id)
            if saved is not None:
                self.current = PlanRun(saved.report, saved.settings, saved.began_at, saved.full_data_at, saved.completed_at, snapshot_id=saved.snapshot_id)
                self.version += 1
                changed = True
        status = await run.io_bound(read_planner_status, self.config.database.path)
        running, error = False, None
        if status is not None:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])).total_seconds()
            if age > 20 or status["state"] == "stopped":
                error = "Planning service is unavailable; displaying the last saved plan."
            else:
                running = status["state"] == "running"
                error = status.get("last_error")
        else:
            error = "Waiting for the planning service."
        if running != self._service_running or error != self.last_error:
            self._service_running, self.last_error, changed = running, error, True
        if changed:
            self._publish()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_for_update()
            except Exception:
                LOGGER.exception("Background plan update check failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
