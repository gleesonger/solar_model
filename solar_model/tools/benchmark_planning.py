"""Benchmark the Markov planner in an isolated process with a bounded deadline."""
from __future__ import annotations

import argparse
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta, timezone
import json
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from time import perf_counter
from typing import Any

from solar_model.config import load_config


def worker(connection: Connection, config_path: str) -> None:
    try:
        from solar_model.planning.plan_optimisation_dynamic_programming import simulate_energy_plan
        from solar_model.planning.scenarios import create_scenarios
        from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval
        config = load_config(config_path)
        device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
        device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100., inverter_max_active_power_kw=4., inverter_max_absorption_power_kw=4., inverter_battery_charge_power_kw=4., inverter_battery_discharge_power_kw=4., grid_max_import_power_kw=float("inf"))
        start = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
        end = start + timedelta(hours=config.energy_plan.num_projection_hours)
        forecast = ForecastData([ForecastInterval(start, end, config.energy_plan.num_projection_hours * .5)], [ForecastInterval(start, end, config.energy_plan.num_projection_hours * .4)])
        scenarios = create_scenarios(forecast, config.energy_plan.scenario_multiplier)
        connection.send({"ready": True})
        began = perf_counter()
        result = simulate_energy_plan(device, DeviceState(5.), scenarios, config, start)
        connection.send({"status": "ok", "elapsed_seconds": perf_counter() - began, "expected_score": result.expected_score, "expected_visible_cost": float(result.expected_results.net_cost.sum())})
    except Exception as error:
        connection.send({"status": "error", "error": str(error)})
    finally:
        connection.close()


def run_case(config_path: str, cap_seconds: float) -> dict[str, Any]:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(child, config_path))
    process.start()
    child.close()
    try:
        if not parent.poll(30):
            return {"status": "N/A", "error": "worker startup timed out"}
        initial = parent.recv()
        if not initial.get("ready"):
            return initial
        if not parent.poll(cap_seconds):
            return {"status": "N/A", "error": "projection exceeded deadline"}
        return parent.recv()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        parent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--cap-seconds", type=float, default=5.)
    parser.add_argument("--output", type=Path, default=Path("markov-benchmark.json"))
    args = parser.parse_args()
    if args.cap_seconds <= 0:
        parser.error("--cap-seconds must be positive")
    config = load_config(args.config)
    result = {"model": "markov", "optimiser": asdict(config.energy_plan.optimiser), **run_case(str(args.config.resolve()), args.cap_seconds)}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
