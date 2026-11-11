"""One owned calculation process, with bounded admission, deadlines and cleanup."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
from threading import Lock
from time import monotonic, sleep
from typing import Any

from solar_model.config import Config


def calculate(operation: str, config: Config) -> dict[str, Any]:
    # Capture the cutoff after the worker receives the job, never before queuing.
    from solar_model.services.planning import generate_plan_report
    from solar_model.services.plan_history import generate_saved_plan
    from solar_model.logging_setup import LOGGER, configure_logging
    configure_logging(config.logging.level)
    began_at = datetime.now(timezone.utc)
    LOGGER.info("Planning calculation started - operation=%s worker_pid=%d", operation, os.getpid())
    if operation == "shared":
        return {"snapshot_id": generate_saved_plan(config, began_at)}
    if operation != "demonstration":
        raise ValueError(f"Unknown planning operation: {operation}")
    return {"report": generate_plan_report(config, began_at), "began_at": began_at, "completed_at": datetime.now(timezone.utc)}


def worker_loop(connection: Connection, handler: Callable[[str, Config], dict[str, Any]]) -> None:
    try:
        while True:
            job = connection.recv()
            if job is None:
                return
            try:
                connection.send({"ok": True, **handler(*job)})
            except Exception as error:
                connection.send({"ok": False, "error": str(error), "error_type": type(error).__name__})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class PlanningWorker:
    def __init__(self, handler: Callable[[str, Config], dict[str, Any]] = calculate) -> None:
        self.handler = handler
        self.process: BaseProcess | None = None
        self.connection: Connection | None = None
        self.deadline: float | None = None
        self._lock = Lock()

    @property
    def running(self) -> bool:
        return self.deadline is not None

    def submit(self, operation: str, config: Config) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("A planning calculation is already running")
            if self.process is None or not self.process.is_alive():
                self._close()
                context = get_context("spawn")
                parent, child = context.Pipe()
                self.process = context.Process(target=worker_loop, args=(child, self.handler), name=f"{operation}-planning-worker", daemon=True)
                self.process.start()
                child.close()
                self.connection = parent
            self.deadline = monotonic() + config.energy_plan.calculation_timeout_seconds
            self.connection.send((operation, config))

    def poll(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.running:
                return None
            try:
                if self.connection.poll():
                    result = self.connection.recv()
                    self.deadline = None
                    return result
                if not self.process.is_alive():
                    raise RuntimeError(f"Planning worker exited (code {self.process.exitcode})")
                if monotonic() >= self.deadline:
                    raise TimeoutError("Planning calculation exceeded its time limit")
            except (EOFError, OSError, RuntimeError, TimeoutError) as error:
                self._close()
                return {"ok": False, "error": str(error), "error_type": type(error).__name__}
            return None

    def wait(self) -> dict[str, Any]:
        while self.running:
            result = self.poll()
            if result is not None:
                return result
            sleep(.05)
        return {"ok": False, "error": "Planning worker stopped", "error_type": "RuntimeError"}

    def _close(self) -> None:
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=1.)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1.)
            self.process.close()
        if self.connection is not None:
            self.connection.close()
        self.process = self.connection = self.deadline = None

    def close(self) -> None:
        with self._lock:
            self._close()
