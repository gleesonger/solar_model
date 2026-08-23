from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType


def main() -> int:
    app_directory = Path(__file__).resolve().parent
    processes = [
        subprocess.Popen([sys.executable, str(app_directory / "main_data_collection.py")]),
        subprocess.Popen([sys.executable, str(app_directory / "main_dashboard.py")]),
    ]
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
            stopped_process = next((process for process in processes if process.poll() is not None), None)
            if stopped_process is not None:
                exit_code = stopped_process.returncode or 1
                break
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
