"""Spawn-safe synthetic process fixtures; never connect to devices or providers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from time import sleep


def report(config, began_at):
    import pandas as pd
    from solar_model.planning.types import SimulationResult, NextAction, OperatingMode
    from solar_model.services.planning import PlanReport, latest_full_update
    start = began_at.replace(microsecond=0)
    frame = pd.DataFrame({
        "starts_at": [start + timedelta(minutes=15 * i) for i in range(4)],
        "ends_at": [start + timedelta(minutes=15 * (i + 1)) for i in range(4)],
        "battery_start_kwh": [4., 4.1, 4.2, 4.3], "battery_end_kwh": [4.1, 4.2, 4.3, 4.4],
        "tariff_name": ["day"] * 4,
    })
    for name in ("solar_kwh", "load_kwh", "net_cost", "grid_import_kwh", "grid_export_kwh", "import_rate", "export_rate", "import_cost", "export_revenue", "cost_score", "protection_score", "total_score", "battery_charge_loss_kwh", "battery_discharge_loss_kwh", "inverter_ac_to_dc_loss_kwh", "inverter_dc_to_ac_loss_kwh", "solar_clipped_kwh", "solar_to_load_kwh", "solar_to_battery_kwh", "solar_to_grid_kwh", "grid_to_load_kwh", "grid_to_battery_kwh", "battery_to_load_kwh", "battery_to_grid_kwh"):
        frame[name] = .1
    results = {"med" if load == solar == "med" else f"{load}_{solar}": frame.copy() for load in ("low", "med", "high") for solar in ("low", "med", "high")}
    actuals = pd.DataFrame({"timestamp": [start], "solar": [2.], "house_load": [1.], "battery": [4.], "grid_import": [.2], "grid_export": [0.]})
    return PlanReport(SimulationResult(NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0.), results), actuals, 20., config.energy_plan.scenario_multiplier, 3., 50., 20., latest_full_update(config.database.path) or start)


def handler(operation, config):
    from solar_model.services.plan_history import persist_report
    preference = config.energy_plan.protection_preference
    if preference == 99:
        raise ValueError("synthetic unavailable plan")
    if preference == 98:
        os._exit(3)
    if preference == 97:
        sleep(3)
    began = datetime.now(timezone.utc)
    value = report(config, began)
    if operation == "shared":
        return {"snapshot_id": persist_report(config, value, began, datetime.now(timezone.utc))}
    return {"report": value, "began_at": began, "completed_at": datetime.now(timezone.utc)}


def serve_planner(path, stop):
    from solar_model.config import load_config
    from solar_model.runtime.planner import PlannerService
    from solar_model.runtime.planning_worker import PlanningWorker
    PlannerService(load_config(path), path, PlanningWorker(handler)).run(stop)


def collect(path, stop):
    from solar_model.storage.connection import create_engine_for_database
    engine = create_engine_for_database(path)
    while not stop.is_set():
        stamp = int(datetime.now(timezone.utc).timestamp())
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT OR IGNORE INTO sigenstor (collected_at_utc, plant_pv_power_kw, plant_load_power_kw, plant_battery_soc_percent, plant_grid_power_kw) VALUES (?, 20000, 10000, 200000, 2000)", (stamp,))
        stop.wait(.2)
