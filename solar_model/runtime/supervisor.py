"""Own collector, planner and dashboard lifetimes independently."""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import sys
import tempfile
from pathlib import Path
from threading import Event
from time import monotonic
from types import FrameType

from solar_model.config import load_config
from solar_model.logging_setup import LOGGER
from solar_model.paths import subprocess_environment
from solar_model.runtime.processes import OwnedProcess
from solar_model.storage.migrations import open_database

MODULES = ("solar_model.runtime.collector", "solar_model.runtime.planner", "solar_model.dashboard.app")


@dataclass
class Service:
    module: str
    process: OwnedProcess | None = None
    started_at: float = 0.
    retry_at: float = 0.
    failures: int = 0


def start_services(environment: dict[str, str]) -> list[Service]:
    services = [Service(module) for module in MODULES]
    try:
        for service in services:
            service.process = OwnedProcess([sys.executable, "-m", service.module], environment)
            service.started_at = monotonic()
    except BaseException:
        for service in services:
            if service.process is not None:
                service.process.stop()
        raise
    return services


def check_services(services: list[Service], environment: dict[str, str]) -> None:
    now = monotonic()
    for service in services:
        if service.process is not None and service.process.poll() is not None:
            code = service.process.poll()
            service.process.stop()  # Reap descendants even when the root has exited.
            service.process = None
            service.failures = 1 if now - service.started_at >= 60 else service.failures + 1
            service.retry_at = now + min(30., 2. ** min(service.failures - 1, 5))
            LOGGER.error("Service %s exited (%s); restart in %.1fs", service.module, code, service.retry_at - now)
        if service.process is None and now >= service.retry_at:
            try:
                service.process = OwnedProcess([sys.executable, "-m", service.module], environment)
                service.started_at = now
            except Exception:
                service.failures += 1
                service.retry_at = now + min(30., 2. ** min(service.failures, 5))
                LOGGER.exception("Unable to restart %s", service.module)


def main() -> int:
    restart_file = Path(tempfile.gettempdir()) / f"solar-model-restart-{os.getpid()}"
    restart_file.unlink(missing_ok=True)
    environment = subprocess_environment() | {"SOLAR_MODEL_RESTART_FILE": str(restart_file)}
    open_database(load_config().database.path).close()
    services = start_services(environment)
    stop = Event()

    def shutdown(number: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stop.is_set():
            if restart_file.exists():
                restart_file.unlink(missing_ok=True)
                for service in services:
                    if service.process is not None:
                        service.process.stop()
                open_database(load_config().database.path).close()
                services = start_services(environment)
            check_services(services, environment)
            stop.wait(.25)
    finally:
        for service in services:
            if service.process is not None:
                service.process.stop()
        restart_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
