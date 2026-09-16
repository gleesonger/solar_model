from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import FrameType

from config import load_config
from database import open_database


def main() -> int:
    app_directory = Path(__file__).resolve().parent
    restart_file = Path(tempfile.gettempdir()) / f"solar-model-restart-{os.getpid()}"
    restart_file.unlink(missing_ok=True)

    def start_processes() -> list[subprocess.Popen]:
        # Reload configuration before each full application restart. Opening
        # the database also applies any pending schema migration before either
        # child begins using it.
        database = open_database(load_config().database.path)
        database.close()
        environment = os.environ | {"SOLAR_MODEL_RESTART_FILE": str(restart_file)}
        return [
            subprocess.Popen(
                [sys.executable, str(app_directory / "main_data_collection.py")],
                env=environment,
            ),
            subprocess.Popen(
                [sys.executable, str(app_directory / "main_dashboard.py")],
                env=environment,
            ),
        ]

    def stop_processes(processes: list[subprocess.Popen]) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    processes = start_processes()
    stopping = False

    def stop(_signal_number: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    exit_code = 0
    try:
        while not stopping:
            if restart_file.exists():
                restart_file.unlink(missing_ok=True)
                stop_processes(processes)
                processes = start_processes()
                continue
            stopped_process = next((process for process in processes if process.poll() is not None), None)
            if stopped_process is not None:
                exit_code = stopped_process.returncode or 1
                break
            time.sleep(0.25)
    finally:
        stop_processes(processes)
        restart_file.unlink(missing_ok=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
