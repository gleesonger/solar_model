"""Headless scheduling, real spawned workers, deadlines and tree ownership."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from pathlib import Path
import sys
import os
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from solar_model.config import load_config
from solar_model.runtime.planner import PlannerService
from solar_model.runtime.planning_worker import PlanningWorker
from solar_model.runtime.processes import OwnedProcess
from solar_model.storage.migrations import open_database
from solar_model.storage.plan_history import list_plans, read_planner_status
from tests.runtime.helpers import handler, collect, serve_planner


def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    text = Path("config.yaml").read_text(encoding="utf-8").replace("path: solar.db", f"path: {tmp_path.as_posix()}/solar.db").replace("plan_rerun_frequency_mins: 5", "plan_rerun_frequency_mins: 0.02")
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    open_database(config.database.path).close()
    return path, config


def eventually(callback, seconds=30):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        value = callback()
        if value:
            return value
        sleep(.1)
    pytest.fail("Timed out waiting for process state")


def test_spawned_worker_deadline_crash_and_recovery(tmp_path):
    path, config = config_file(tmp_path)
    worker = PlanningWorker(handler)
    try:
        before = datetime.now(timezone.utc)
        worker.submit("demonstration", config)
        with pytest.raises(RuntimeError, match="already running"):
            worker.submit("shared", config)
        assert worker.wait()["began_at"] >= before
        initial_pid = worker.process.pid
        timeout = replace(config, energy_plan=replace(config.energy_plan, protection_preference=97, calculation_timeout_seconds=.2))
        worker.submit("demonstration", timeout)
        assert "time limit" in worker.wait()["error"]
        assert worker.process is None
        worker.submit("demonstration", config)
        assert worker.wait()["ok"]
        assert worker.process.pid != initial_pid
        crash = replace(config, energy_plan=replace(config.energy_plan, protection_preference=98))
        worker.submit("demonstration", crash)
        assert not worker.wait()["ok"]
        worker.submit("demonstration", config)
        assert worker.wait()["ok"]
    finally:
        worker.close()


def test_planner_publishes_without_dashboard_and_resumes_after_restart(tmp_path):
    path, config = config_file(tmp_path)
    context = get_context("spawn")
    stop = context.Event()
    collector = context.Process(target=collect, args=(config.database.path, stop))
    planner = context.Process(target=serve_planner, args=(str(path), stop))
    collector.start()
    planner.start()
    try:
        eventually(lambda: len(list_plans(config.database.path)) >= 2)
        first = list_plans(config.database.path)[0]
        status = read_planner_status(config.database.path)
        assert status["pid"] == planner.pid and status["worker_pid"]
        # The dashboard has never been imported or started in either service.
        assert first.metadata["full_data_at"]
        stop.set()
        collector.join(5)
        planner.join(5)
        assert collector.exitcode == planner.exitcode == 0
        assert read_planner_status(config.database.path)["state"] == "stopped"
        # A fresh runtime reads persisted cadence state and catches the next reading.
        stop.clear()
        collector = context.Process(target=collect, args=(config.database.path, stop))
        planner = context.Process(target=serve_planner, args=(str(path), stop))
        collector.start()
        planner.start()
        eventually(lambda: list_plans(config.database.path)[0].snapshot_id != first.snapshot_id)
    finally:
        stop.set()
        for process in (collector, planner):
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)


def test_owned_process_kills_descendants_even_after_root_exit(tmp_path):
    marker = tmp_path / "child.pid"
    child = "import time; time.sleep(120)"
    script = f"import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',{child!r}]); Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(120)"
    owned = OwnedProcess([sys.executable, "-c", script])
    try:
        eventually(marker.exists, seconds=5)
        pid = int(marker.read_text())
        owned.process.kill()
        owned.process.wait(5)
        owned.stop()
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.WinDLL("kernel32")
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)
                assert code.value != 259  # STILL_ACTIVE
    finally:
        owned.stop()


def test_supervisor_restarts_only_failed_service_with_backoff(monkeypatch):
    from solar_model.runtime import supervisor
    stopped = []
    healthy = SimpleNamespace(poll=lambda: None, stop=lambda: stopped.append("healthy"))
    failed = SimpleNamespace(poll=lambda: 3, stop=lambda: stopped.append("failed"))
    services = [supervisor.Service("collector", healthy), supervisor.Service("planner", failed)]
    monkeypatch.setattr(supervisor, "monotonic", lambda: 10.)
    launched = []
    monkeypatch.setattr(supervisor, "OwnedProcess", lambda command, env: launched.append(command[-1]) or healthy)
    supervisor.check_services(services, {})
    assert stopped == ["failed"] and launched == []
    monkeypatch.setattr(supervisor, "monotonic", lambda: 12.)
    supervisor.check_services(services, {})
    assert launched == ["planner"] and services[0].process is healthy


def test_real_supervisor_keeps_collector_and_planner_alive_when_dashboard_crashes(tmp_path):
    path, config = config_file(tmp_path)
    repo = Path(__file__).resolve().parents[2]
    (tmp_path / "collector_fixture.py").write_text("from threading import Event\nfrom solar_model.config import load_config\nfrom tests.runtime.helpers import collect\nif __name__ == '__main__':\n    collect(load_config().database.path, Event())\n")
    (tmp_path / "planner_fixture.py").write_text("from threading import Event\nfrom tests.runtime.helpers import serve_planner\nif __name__ == '__main__':\n    serve_planner('config.yaml', Event())\n")
    marker = tmp_path / "dashboard.pid"
    (tmp_path / "dashboard_fixture.py").write_text(f"import os,time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\nwhile True: time.sleep(1)\n")
    script = f"import os; os.chdir({str(tmp_path)!r}); from solar_model.runtime import supervisor; supervisor.MODULES=('collector_fixture','planner_fixture','dashboard_fixture'); supervisor.main()"
    environment = os.environ | {"PYTHONPATH": str(repo) + os.pathsep + str(tmp_path)}
    owned = OwnedProcess([sys.executable, "-c", script], environment)
    try:
        eventually(lambda: marker.exists() and len(list_plans(config.database.path)) >= 1)
        old_pid = int(marker.read_text())
        status = read_planner_status(config.database.path)
        first = list_plans(config.database.path)[0].snapshot_id
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(old_pid), "/F"], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.kill(old_pid, signal.SIGKILL)
        eventually(lambda: int(marker.read_text()) != old_pid)
        eventually(lambda: list_plans(config.database.path)[0].snapshot_id != first)
        assert read_planner_status(config.database.path)["pid"] == status["pid"]
        assert owned.poll() is None
    finally:
        owned.stop()


def test_production_worker_calculates_and_publishes_real_plan_from_temporary_inputs(tmp_path):
    from dataclasses import fields
    import pandas as pd
    from solar_model.planning.types import DeviceInfo
    from solar_model.storage.schema import ForecastSolarSample, SigenStorDevice, SigenStorHourlySample, SigenStorModbusSample
    from solar_model.services.plan_history import read_saved_run
    path, config = config_file(tmp_path)
    config = replace(config, timezone="UTC", forecast=replace(config.forecast, solar=replace(config.forecast.solar, arrays=(config.forecast.solar.arrays[0],))), energy_plan=replace(config.energy_plan, num_projection_hours=25, num_visible_projection_hours=1, protection_preference=0., calculation_timeout_seconds=30., optimiser=replace(config.energy_plan.optimiser, compile_model_to_native_code=False, grid_step_percent=25, power_divisions=1)))
    config = replace(config, dashboard=replace(config.dashboard, actuals_to_forecast={"pv1": config.forecast.solar.arrays[0].name}))
    start = datetime.now(timezone.utc).replace(microsecond=0)
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=18., battery_state_of_health_percent=100., inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5., inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.)
    database = open_database(config.database.path)
    try:
        with database.sessions.begin() as session:
            for field in fields(device):
                session.add(SigenStorDevice(collected_at_utc=(start - timedelta(minutes=1)).isoformat(), collected_at_local=start.isoformat(), variable=field.name, variable_name=field.name, value=str(getattr(device, field.name)), unit=""))
            midnight = start.replace(hour=0, minute=0, second=0)
            for stamp in pd.date_range(midnight - timedelta(days=2), midnight, freq="h", inclusive="left"):
                session.add(SigenStorHourlySample(hour_start_utc=stamp.isoformat(), import_tariff_band="day", sample_count=1, house_load_kwh_period=.35))
            for hour in range(28):
                session.add(ForecastSolarSample(collected_at_utc=(start - timedelta(minutes=1)).isoformat(), forecast_time=(start.replace(minute=0) + timedelta(hours=hour)).isoformat(), **{f"panel_{config.forecast.solar.arrays[0].panel_id}_adj_watt_hours": hour * 1000.}))
            session.add(SigenStorModbusSample(collected_at_utc=start.isoformat(), plant_battery_soc_percent=50., plant_pv_power_kw=1., plant_load_power_kw=.35))
        worker = PlanningWorker()
        try:
            before = datetime.now(timezone.utc)
            worker.submit("shared", config)
            completed = worker.wait()
            assert completed["ok"], completed
            saved = read_saved_run(config, completed["snapshot_id"])
            assert saved.began_at >= before
            assert saved.full_data_at == start
            assert len(saved.report.simulation.results) == 9
            assert len(saved.report.details["Compact"]) == 9
            assert saved.report.simulation.results["med"].battery_start_kwh.iloc[0] == 9.
        finally:
            worker.close()
    finally:
        database.close()
