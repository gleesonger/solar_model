"""Server-owned plans, refreshed when new full SigEnergy readings are committed."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from yaml import YAMLError

from nicegui import run
from nicegui.run import SubprocessException

from solar_model.config import Config, EnergyPlanConfig, load_config, parse_energy_plan_yaml, save_energy_plan_yaml
from solar_model.dashboard.planning_worker import ensure_planning_worker
from solar_model.logging_setup import LOGGER
from solar_model.planning.types import NoFeasiblePlanError
from solar_model.services.planning import PlanReport, generate_plan_report, latest_full_update


@dataclass(frozen=True)
class PlanRun:
    report: PlanReport
    settings: EnergyPlanConfig
    began_at: datetime
    full_data_at: datetime
    completed_at: datetime
    demonstration: bool = False


class PlanStore:
    """Keep one shared actual plan; user calculations remain private demonstrations."""

    def __init__(self, config: Config, config_path: str = "config.yaml") -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.current: PlanRun | None = None
        self.version = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._stop = asyncio.Event()
        self._observed_data_at: datetime | None = None
        self._baseline_established = False
        self._disk_signature: tuple[int, int] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

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

    async def _calculate(self, config: Config, demonstration: bool = False) -> PlanRun:
        began_at = datetime.now(timezone.utc)
        await ensure_planning_worker()
        report = await run.cpu_bound(generate_plan_report, config, began_at, None)
        if report is None or report.full_data_at is None:
            raise RuntimeError("The plan calculation did not return a full-data timestamp")
        return PlanRun(report, config.energy_plan, began_at, report.full_data_at, datetime.now(timezone.utc), demonstration)

    async def demonstrate(self, settings: EnergyPlanConfig) -> PlanRun:
        return await self._calculate(replace(self.config, energy_plan=settings), demonstration=True)

    async def _refresh(self, config: Config) -> PlanRun | None:
        self.last_error = None
        self._publish()
        try:
            completed = await self._calculate(config)
            self.current = completed
            self.version += 1
            LOGGER.info("Shared plan completed: began_at=%s full_data_at=%s completed_at=%s", completed.began_at, completed.full_data_at, completed.completed_at)
            return completed
        except Exception as error:
            if isinstance(error, SubprocessException) and error.original_type == NoFeasiblePlanError.__name__:
                error = NoFeasiblePlanError(error.original_message)
            self.last_error = str(error)
            if isinstance(error, NoFeasiblePlanError):
                LOGGER.warning("Unable to calculate shared plan: %s", error)
            else:
                LOGGER.exception("Shared plan refresh failed")
            return None
        finally:
            self._task = None
            self._publish()

    def _start_refresh(self) -> asyncio.Task:
        if not self.running:
            self._task = asyncio.create_task(self._refresh(self.config), name="shared energy plan")
        return self._task

    async def check_for_update(self) -> None:
        await self.reload_settings()
        latest = await run.io_bound(latest_full_update, self.config.database.path)
        if not self._baseline_established:
            self._observed_data_at = latest
            self._baseline_established = True
            LOGGER.info("Background plan waiting for a new full SigEnergy reading: latest_full_data_at=%s", latest)
            return
        if self._stop.is_set() or latest is None or self.running or (self._observed_data_at is not None and latest <= self._observed_data_at):
            return
        self._observed_data_at = latest
        previous = self.current
        threshold_seconds = max(0., self.config.energy_plan.plan_rerun_frequency_mins * 60 - 15)
        if previous is None or previous.settings != self.config.energy_plan or (latest - previous.full_data_at).total_seconds() >= threshold_seconds:
            LOGGER.info("Background plan starting: full_data_at=%s rerun_frequency_mins=%g", latest, self.config.energy_plan.plan_rerun_frequency_mins)
            self._start_refresh()

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
        if self.running:
            await asyncio.shield(self._task)
