r"""Isolated review reproductions; no production database or hardware access.

Run from repository root:
  .venv\Scripts\python.exe documentation/reviews/system-review-architecture-evidence.py
"""
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import database
import docker_start
from config import load_config
from dashboard.live import (
    LivePowerCollector, LIVE_POWER_METRICS, LIVE_BATTERY_METRICS, LIVE_TODAY_METRICS,
)

root = Path(tempfile.mkdtemp(prefix="solar-architecture-evidence-"))
print("Temporary evidence directory:", root)
path = root / "interrupted.db"
db = database.open_database(path)
with db.engine.begin() as connection:
    connection.exec_driver_sql(
        "INSERT INTO sigenstor(collected_at_utc,plant_pv_power_kw,import_tariff_band) "
        "VALUES(1800000000,20000,'day')"
    )
    connection.exec_driver_sql("DROP TABLE sigenstor_hourly")
db.close()
with patch.object(database.SolarDatabase, "backfill_hourly_rollup", side_effect=RuntimeError("interrupted backfill")):
    try:
        database.open_database(path)
    except RuntimeError:
        pass
db = database.open_database(path)
with db.engine.connect() as connection:
    print("A1 reopened raw/hourly:", [connection.exec_driver_sql("SELECT count(*) FROM " + table).scalar()
                                       for table in ("sigenstor", "sigenstor_hourly")])
db.backfill_hourly_rollup()
db.backfill_hourly_rollup()
with db.engine.connect() as connection:
    print("A1 manual repeated repair:", connection.exec_driver_sql("SELECT sample_count FROM sigenstor_hourly").all())
db.close()

config = load_config()
def collector():
    return LivePowerCollector(config.actuals.modbus, config.timezone, str(root / "unused.db"), 1, ("pv1",))

live = collector()
values = {"solar": 2.0, "today_solar": 1.0}
readings = {"inverter_pv1_voltage": (400.0, "V", False), "inverter_pv1_current": (5.0, "A", False)}
with patch("dashboard.live.data.load_pv_array_energy_day_totals", side_effect=RuntimeError("temporary DB lock")):
    try:
        live._add_live_pv_array_values(values, readings, "2026-10-01T12:00:00+01:00")
    except RuntimeError:
        pass
with patch("dashboard.live.data.load_pv_array_energy_day_totals", return_value={1: 0.0}) as loader:
    try:
        live._add_live_pv_array_values(values, readings, "2026-10-01T12:00:01+01:00")
    except KeyError as error:
        print("A2 PV next attempt:", repr(error), "baseline retries:", loader.call_count)

live = collector()
readings = {metric: (10.0, "", False) for metric in LIVE_POWER_METRICS | LIVE_BATTERY_METRICS | LIVE_TODAY_METRICS}
readings.update({"plant_grid_import_total_kwh": (100.0, "kWh", True), "plant_grid_export_total_kwh": (50.0, "kWh", True)})
with patch("dashboard.live.data.load_grid_energy_day_start", side_effect=RuntimeError("temporary DB lock")):
    try:
        live._power_values(readings, "2026-10-01T12:00:00+01:00")
    except RuntimeError:
        pass
with patch("dashboard.live.data.load_grid_energy_day_start", return_value={"today_grid_import": 90.0, "today_grid_export": 45.0}) as loader, patch.object(live, "_add_live_pv_array_values"):
    result = live._power_values(readings, "2026-10-01T12:00:01+01:00")
    print("A2 grid import/export:", result["today_grid_import"], result["today_grid_export"], "expected: 10, 5; retries:", loader.call_count)

child = Mock()
child.poll.return_value = None
with patch.object(docker_start, "load_config") as config_loader, patch.object(docker_start, "open_database"), patch.object(docker_start.subprocess, "Popen", side_effect=[child, OSError("second child launch failed")]):
    config_loader.return_value.database.path = str(root / "unused.db")
    try:
        docker_start.main()
    except OSError:
        print("A3 first child terminated/waited:", child.terminate.called, child.wait.called)
