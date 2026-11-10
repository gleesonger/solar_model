"""Headless, supervised owner of scheduled planning and plan publication."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
from threading import Event
from time import monotonic
from types import FrameType

from yaml import YAMLError

from solar_model.config import Config, energy_plan_yaml, load_config
from solar_model.logging_setup import LOGGER, configure_logging
from solar_model.runtime.planning_worker import PlanningWorker
from solar_model.services.planning import latest_full_update
from solar_model.storage.migrations import open_database
from solar_model.storage.plan_history import load_plan, write_planner_status


class PlannerService:
    def __init__(self, config: Config, config_path: str = "config.yaml", worker: PlanningWorker | None = None) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.worker = worker or PlanningWorker()
        self.observed: datetime | None = None
        self.initialised = False
        self.signature: tuple[int, int] | None = None
        self.last_data_at: datetime | None = None
        self.last_settings: str | None = None
        self.active_settings: str | None = None
        self.snapshot_id: str | None = None
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        self.last_status = -float("inf")

    def reload_settings(self) -> None:
        stat = self.config_path.stat()
        signature = stat.st_mtime_ns, stat.st_size
        if signature == self.signature:
            return
        self.signature = signature
        try:
            updated = load_config(self.config_path)
        except (ValueError, YAMLError) as error:
            LOGGER.warning("Unable to reload energy_plan; retaining last valid settings: %s", error)
            return
        self.config = replace(self.config, energy_plan=updated.energy_plan)

    def publish_status(self, force: bool = False) -> None:
        if not force and monotonic() - self.last_status < 5:
            return
        write_planner_status(self.config.database.path, {
            "pid": os.getpid(),
            "worker_pid": self.worker.process.pid if self.worker.process else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "state": "running" if self.worker.running else "waiting",
            "snapshot_id": self.snapshot_id,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
        })
        self.last_status = monotonic()

    def step(self) -> None:
        self.reload_settings()
        finished = self.worker.poll()
        if finished is not None:
            if finished["ok"]:
                stored = load_plan(self.config.database.path, finished["snapshot_id"])
                if stored is None:
                    raise RuntimeError("Worker returned an unavailable saved plan")
                self.snapshot_id = stored.snapshot_id
                self.last_data_at = datetime.fromisoformat(stored.metadata["full_data_at"])
                self.last_settings = self.active_settings
                self.last_success_at = stored.saved_at.isoformat()
                self.last_error = None
                LOGGER.info("Scheduled plan published: snapshot_id=%s", self.snapshot_id)
            else:
                self.last_error = finished["error"]
                LOGGER.warning("Scheduled plan failed: %s", self.last_error)
            self.publish_status(force=True)
        latest = latest_full_update(self.config.database.path)
        if not self.initialised:
            stored = load_plan(self.config.database.path)
            if stored is not None:
                self.snapshot_id = stored.snapshot_id
                self.last_data_at = datetime.fromisoformat(stored.metadata["full_data_at"])
                self.last_settings = stored.metadata["settings"]
                self.last_success_at = stored.saved_at.isoformat()
            self.observed, self.initialised = latest, True
        elif latest is not None and not self.worker.running and (self.observed is None or latest > self.observed):
            self.observed = latest
            settings = energy_plan_yaml(self.config.energy_plan)
            threshold = max(0., self.config.energy_plan.plan_rerun_frequency_mins * 60 - 15)
            if self.last_data_at is None or settings != self.last_settings or (latest - self.last_data_at).total_seconds() >= threshold:
                self.last_error = None
                self.active_settings = settings
                self.worker.submit("shared", self.config)
                self.publish_status(force=True)
        self.publish_status()

    def run(self, stop: Event | None = None) -> None:
        stop = stop or Event()
        try:
            while not stop.is_set():
                try:
                    self.step()
                except Exception as error:
                    self.last_error = str(error)
                    LOGGER.exception("Planning service update failed")
                    self.worker.close()
                stop.wait(1.)
        finally:
            self.worker.close()
            write_planner_status(self.config.database.path, {"pid": os.getpid(), "state": "stopped", "updated_at": datetime.now(timezone.utc).isoformat(), "last_error": self.last_error})


def main(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    configure_logging(config.logging.level)
    open_database(config.database.path).close()
    stop = Event()

    def shutdown(number: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    PlannerService(config, config_path).run(stop)


if __name__ == "__main__":
    main()
