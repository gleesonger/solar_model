"""Run 19 independent nine-scenario sensitivity cases with a hard five-second cap."""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import platform
import traceback
from pathlib import Path
from time import perf_counter, process_time


CASES = (
    "baseline", "battery_4", "battery_7.9", "battery_8.1", "battery_14",
    "solar_0.75", "solar_0.95", "solar_1.05", "solar_1.25",
    "load_0.75", "load_0.95", "load_1.05", "load_1.25",
    "preference_0", "preference_50", "reserve_2", "reserve_6",
    "tariff_flat", "tariff_peak_1.5",
)
STEPS = [15, 10, 15, 15] + [15] * 20 + [60] * 18 + [5]
CAP_SECONDS = 5.0


def _worker(connection, case: str, config_path: str) -> None:
    # Heavy imports and synthetic inputs are prepared outside the timed budget.
    try:
        from dataclasses import asdict, fields, replace
        from datetime import datetime, timedelta, timezone
        import math
        from unittest.mock import patch

        import cvxpy as cp
        import numpy as np
        from solar_model.config import load_config
        from solar_model.planning import optimisation
        from solar_model.planning.scenarios import create_scenarios
        from solar_model.planning.types import (
            DeviceInfo, DeviceState, ForecastData, ForecastInterval,
        )

        config = load_config(Path(config_path))
        start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
        timestamp = start.replace(minute=0)
        coverage_end = timestamp + timedelta(hours=49)
        solar, load = [], []
        while timestamp < coverage_end:
            hour = timestamp.hour + timestamp.minute / 60
            generation = (
                1.8 * max(0., math.sin(math.pi * (hour - 8) / 9)) / 2
                if 8 <= hour <= 17 else 0.
            )
            power = 1.2 if 7 <= hour < 9 else 1.4 if 17 <= hour < 21 else .35
            end = timestamp + timedelta(minutes=30)
            solar.append(ForecastInterval(timestamp, end, generation))
            load.append(ForecastInterval(timestamp, end, power / 2))
            timestamp = end

        initial_battery = 8.
        if case.startswith("battery_"):
            initial_battery = float(case.split("_", 1)[1])
        elif case.startswith(("solar_", "load_")):
            scale = float(case.split("_", 1)[1])
            if case.startswith("solar_"):
                solar = [replace(interval, kwh=interval.kwh * scale) for interval in solar]
            else:
                load = [replace(interval, kwh=interval.kwh * scale) for interval in load]
        elif case.startswith(("preference_", "reserve_")):
            value = float(case.split("_", 1)[1])
            key = "protection_preference" if case.startswith("preference_") else "preferred_reserve_kwh"
            config = replace(config, energy_plan=replace(config.energy_plan, **{key: value}))
        elif case.startswith("tariff_"):
            mean = optimisation._reference_import_rate(config, start, start + timedelta(hours=24))
            periods = tuple(
                replace(period, import_=tuple(
                    replace(rule, rate=mean) if case == "tariff_flat"
                    else replace(rule, rate=rule.rate * 1.5) if rule.name == "peak" else rule
                    for rule in period.import_
                )) for period in config.tariffs.periods
            )
            config = replace(config, tariffs=replace(config.tariffs, periods=periods))

        scenarios = create_scenarios(ForecastData(solar, load))
        assert len(scenarios) == 9
        device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
        device = replace(
            device, system_battery_capacity_kwh=18., battery_state_of_health_percent=100.,
            inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
            inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.,
            charge_efficiency=.95, discharge_efficiency=.95,
            battery_discharge_cut_off_soc_percent=10., battery_charge_cut_off_soc_percent=100.,
        )

        def projection_intervals(cfg, when):
            intervals = []
            for minutes in STEPS:
                end = when + timedelta(minutes=minutes)
                intervals.append((when, end))
                when = end
            return intervals

        metrics = {}
        original_solve = cp.Problem.solve

        def timed_solve(problem, *args, **kwargs):
            metrics["build_seconds"] = perf_counter() - begin
            kwargs["warm_start"] = False
            value = original_solve(problem, *args, **kwargs)
            metrics.update(
                compile_seconds=problem.compilation_time,
                highs_solve_seconds=problem.solver_stats.solve_time,
                objective=float(value), solver_status=problem.status,
                mip_node_count=getattr(problem.solver_stats.extra_stats, "mip_node_count", None),
                mip_gap=getattr(problem.solver_stats.extra_stats, "mip_gap", None),
            )
            assert problem.status == cp.OPTIMAL, problem.status
            return value

        connection.send({"event": "ready"})
        connection.recv()  # Parent arms the process deadline before starting work.
        begin, cpu_begin = perf_counter(), process_time()
        with patch.object(optimisation, "_projection_intervals", projection_intervals), \
                patch.object(cp.Problem, "solve", timed_solve):
            result = optimisation.simulate_energy_plan(
                device, DeviceState(initial_battery), scenarios, config, start,
            )
        metrics["simulation_seconds"] = perf_counter() - begin
        assert len(result.results) == 9
        for frame in result.results.values():
            assert len(frame) == 43
            f = frame.drop(columns=["starts_at", "ends_at", "tariff_name"]).astype(float)
            np.testing.assert_allclose(
                f.battery_end_kwh,
                f.battery_start_kwh + f.solar_to_battery_kwh + f.grid_to_battery_kwh
                - f.battery_to_load_kwh - f.battery_to_grid_kwh - f.battery_to_ac_loss_kwh,
                atol=1e-5,
            )
            np.testing.assert_allclose(
                f.solar_kwh, f.solar_to_load_kwh + f.solar_to_battery_kwh
                + f.solar_to_battery_loss_kwh + f.solar_to_grid_kwh + f.solar_clipped_kwh,
                atol=1e-5,
            )
            np.testing.assert_allclose(
                f.load_kwh, f.solar_to_load_kwh + f.battery_to_load_kwh + f.grid_to_load_kwh,
                atol=1e-5,
            )
            np.testing.assert_allclose(
                f.net_cost, f.grid_import_kwh * f.import_rate - f.grid_export_kwh * f.export_rate,
                atol=1e-5,
            )
            assert (f.battery_end_kwh >= f.minimum_battery_kwh - 1e-5).all()
            assert (f.battery_end_kwh <= f.maximum_battery_kwh + 1e-5).all()
            assert not ((f.solar_to_battery_kwh + f.grid_to_battery_kwh > 1e-5)
                        & (f.battery_to_load_kwh + f.battery_to_grid_kwh > 1e-5)).any()
            durations = [(b - a).total_seconds() / 60 for a, b in zip(frame.starts_at, frame.ends_at)]
            np.testing.assert_allclose(durations, STEPS)
        metrics.update(
            total_seconds=perf_counter() - begin, cpu_seconds=process_time() - cpu_begin,
            expected_cost=float(sum(s.probability * result.results[s.name].net_cost.sum() for s in scenarios)),
            next_action=asdict(result.next_action), validation="passed",
        )
        connection.send({"event": "result", "status": "completed", **metrics})
    except Exception:
        connection.send({"event": "result", "status": "error", "error": traceback.format_exc()})
    finally:
        connection.close()


def _run_case(context, case: str, config_path: str) -> dict:
    parent, child = context.Pipe()
    process = context.Process(target=_worker, args=(child, case, config_path))
    process.start()
    child.close()
    try:
        if not parent.poll(30):
            return {"status": "error", "error": "Worker did not prepare inputs within 30 seconds"}
        message = parent.recv()
        if message["event"] != "ready":
            return message
        deadline = perf_counter() + CAP_SECONDS
        parent.send("start")
        if not parent.poll(max(0., deadline - perf_counter())):
            return {"status": "timeout", "total_seconds": None}
        message = parent.recv()
        if perf_counter() > deadline:
            return {"status": "timeout", "total_seconds": None}
        return message
    except EOFError:
        return {"status": "error", "error": f"Worker exited without a result (exit code {process.exitcode})"}
    finally:
        # A separate process lets the controller stop native HiGHS calls reliably.
        if process.is_alive():
            process.kill()
        process.join()
        parent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, default=Path("planning-benchmark.json"))
    args = parser.parse_args()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    report = {
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "cap_seconds": CAP_SECONDS, "timesteps_minutes": STEPS,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "model_sha256": hashlib.sha256(Path(__file__).parents[1].joinpath("planning/optimisation.py").read_bytes()).hexdigest(),
        "cases": [],
    }
    context = mp.get_context("spawn")
    print(f"{'Case':<20} {'Status':<10} {'Total s':>9} {'Build s':>9} {'Compile s':>10} {'HiGHS s':>9}", flush=True)
    for case in CASES:
        record = {"case": case, **_run_case(context, case, str(args.config.resolve()))}
        record.pop("event", None)
        report["cases"].append(record)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        times = [f"{record[key]:.3f}" if record.get(key) is not None else "N/A"
                 for key in ("total_seconds", "build_seconds", "compile_seconds", "highs_solve_seconds")]
        print(f"{case:<20} {record['status']:<10} {times[0]:>9} {times[1]:>9} {times[2]:>10} {times[3]:>9}", flush=True)
        if record["status"] == "error":
            print(record["error"], flush=True)
    print(f"Saved {len(CASES)} single-run cases to {args.output}", flush=True)
    if any(record["status"] == "error" for record in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
