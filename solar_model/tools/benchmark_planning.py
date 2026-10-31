"""Compare planning models on isolated synthetic cases with a hard process deadline."""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import platform
import signal
import subprocess
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


def _worker(connection, case: str, config_path: str, grid: str = "historical", model: str = "lp", grid_step_percent: float | None = None, power_divisions: int | None = None, refine_band_percent: float | None = None, refinement_stages: list[tuple[float, int, float]] | None = None, dp_backend: str | None = None) -> None:
    if os.name != "nt":
        os.setsid()  # The deadline controller must also stop nested mode workers.
    # Heavy imports and synthetic inputs are prepared outside the timed budget.
    try:
        from contextlib import ExitStack
        from dataclasses import asdict, fields, replace
        from datetime import datetime, timedelta, timezone
        import math
        from unittest.mock import patch

        import numpy as np
        from solar_model.config import load_config
        if model == "dp":
            from solar_model.planning import plan_optimisation_dynamic_programming as dynamic_programming
            calculator = dynamic_programming
        else:
            import cvxpy as cp
            from solar_model.planning import plan_optimisation_milp as optimisation
            calculator = optimisation
        from solar_model.planning.scenarios import create_scenarios
        from solar_model.planning.types import (
            DeviceInfo, DeviceState, ForecastData, ForecastInterval,
        )

        config = load_config(Path(config_path))
        if model == "dp" and dp_backend is not None:
            settings = replace(config.energy_plan.optimiser_specific.dynamic_programing, use_numba=dp_backend == "numba")
            config = replace(config, energy_plan=replace(config.energy_plan, optimiser_specific=replace(config.energy_plan.optimiser_specific, dynamic_programing=settings)))
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
            mean = (calculator.reference_import_rate if model == "dp" else calculator._reference_import_rate)(config, start, start + timedelta(hours=24))
            periods = tuple(
                replace(period, import_=tuple(
                    replace(rule, rate=mean) if case == "tariff_flat"
                    else replace(rule, rate=rule.rate * 1.5) if rule.name == "peak" else rule
                    for rule in period.import_
                )) for period in config.tariffs.periods
            )
            config = replace(config, tariffs=replace(config.tariffs, periods=periods))

        scenarios = create_scenarios(ForecastData(solar, load), config.scenarios)
        assert len(scenarios) == 9
        device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
        device = replace(
            device, system_battery_capacity_kwh=18., battery_state_of_health_percent=100.,
            inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
            inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.,
            charge_efficiency=.95, discharge_efficiency=.95,
            battery_discharge_cut_off_soc_percent=10., battery_charge_cut_off_soc_percent=100.,
        )

        def historical_intervals(cfg, when):
            intervals = []
            for minutes in STEPS:
                end = when + timedelta(minutes=minutes)
                intervals.append((when, end))
                when = end
            return intervals

        if grid == "quarter-hour":
            periods = [(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1))) for i in range(96)]
        else:
            periods = historical_intervals(config, start) if grid == "historical" else (calculator.projection_intervals(config, start) if model == "dp" else calculator._projection_intervals(config, start))
        steps = [(end - begin).total_seconds() / 60 for begin, end in periods]

        def projection_intervals(cfg, when):
            return list(periods)

        metrics = {"dp_settings": asdict(config.energy_plan.optimiser_specific.dynamic_programing) if model == "dp" else None, "build_seconds": None, "compile_seconds": None, "highs_solve_seconds": None}
        original_solve = cp.Problem.solve if model == "lp" else None

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

        if model == "dp":
            use_numba = config.energy_plan.optimiser_specific.dynamic_programing.use_numba
            metrics["numba_initialisation_in_timing"] = use_numba
            metrics["numpy_version"] = np.__version__
            if use_numba:
                from importlib.metadata import version
                metrics["numba_version"] = version("numba")
        connection.send({"event": "ready"})
        connection.recv()  # Parent arms the process deadline before starting work.
        begin, cpu_begin = perf_counter(), process_time()
        simulate = calculator.simulate_energy_plan
        with ExitStack() as stack:
            stack.enter_context(patch.object(calculator, "projection_intervals" if model == "dp" else "_projection_intervals", projection_intervals))
            if model == "lp":
                stack.enter_context(patch.object(cp.Problem, "solve", timed_solve))
            options = {"grid_step_percent": grid_step_percent, "power_divisions": power_divisions} if model == "dp" else {}
            initial_stage = config.energy_plan.optimiser_specific.dynamic_programing.refinement_stages[0]
            stages = refinement_stages or ([(initial_stage.grid_step_percent if grid_step_percent is None else grid_step_percent, initial_stage.power_divisions if power_divisions is None else power_divisions, refine_band_percent)] if refine_band_percent is not None else [])
            if not stages:
                result = simulate(device, DeviceState(initial_battery), scenarios, config, start, **options)
            else:
                coarse_at = perf_counter()
                coarse = simulate(device, DeviceState(initial_battery), scenarios, config, start, grid_step_percent=initial_stage.grid_step_percent, power_divisions=initial_stage.power_divisions)
                metrics["coarse_seconds"] = perf_counter() - coarse_at
                coarse_score = sum(s.probability * (coarse.results[s.name].total_score.sum() + coarse.lookahead_results[s.name].total_score.sum()) for s in scenarios)
                capacity = device.system_battery_capacity_kwh * device.battery_state_of_health_percent / 100
                result, best_score = coarse, coarse_score
                stage_records = []
                for stage_index, (stage_grid, stage_divisions, band) in enumerate(stages, 1):
                    bounds = {}
                    for scenario in scenarios:
                        energies = [*result.results[scenario.name].battery_end_kwh, *result.lookahead_results[scenario.name].battery_end_kwh]
                        bounds[scenario.name] = [(max(0., energy / capacity - band / 100), min(1., energy / capacity + band / 100)) for energy in energies]
                    refined_at = perf_counter()
                    try:
                        refined = simulate(device, DeviceState(initial_battery), scenarios, config, start, battery_bounds=bounds, grid_step_percent=stage_grid, power_divisions=stage_divisions)
                    except ValueError as error:
                        if str(error) != "unable to find a feasible energy plan on the dynamic-programming grid":
                            raise
                        refined = None
                    stage_seconds = perf_counter() - refined_at
                    refined_score = None if refined is None else sum(s.probability * (refined.results[s.name].total_score.sum() + refined.lookahead_results[s.name].total_score.sum()) for s in scenarios)
                    accepted = bool(refined_score is not None and refined_score > best_score)
                    if accepted:
                        result, best_score = refined, refined_score
                    stage_records.append({"grid_step_percent": stage_grid, "power_divisions": stage_divisions, "band_percent": band, "seconds": stage_seconds, "score": None if refined_score is None else float(refined_score), "retained_score": float(best_score), "accepted": accepted, "status": "infeasible" if refined is None else "completed"})
                    print(f"dp_refinement stage={stage_index} accepted={accepted} seconds={stage_seconds:.6f} retained_score={best_score:.9f}", flush=True)
                metrics.update(coarse_full_score=float(coarse_score), refinement_stages=stage_records, refinement_seconds=sum(stage["seconds"] for stage in stage_records), refined_full_score=stage_records[-1]["score"], selected_pass="coarse" if result is coarse else "refined", refinement_status=stage_records[-1]["status"])
                print(f"dp_refinement end: selected_pass={metrics['selected_pass']} coarse_seconds={metrics['coarse_seconds']:.6f} refinement_seconds={metrics['refinement_seconds']:.6f}", flush=True)
        metrics["simulation_seconds"] = perf_counter() - begin
        assert len(result.results) == 9
        for frame in result.results.values():
            assert len(frame) == len(steps)
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
            np.testing.assert_allclose(durations, steps)
        # Compare the native objective with independently reconstructed reports.
        scenario_metrics = {}
        for scenario in scenarios:
            visible = result.results[scenario.name]
            hidden = result.lookahead_results[scenario.name]
            full_cost = float(visible.net_cost.sum() + hidden.net_cost.sum())
            full_score = float(visible.total_score.sum() + hidden.total_score.sum())
            scenario_metrics[scenario.name] = {
                "probability": scenario.probability / sum(s.probability for s in scenarios),
                "visible_cost": float(visible.net_cost.sum()), "full_cost": full_cost,
                "full_score": full_score, "visible_end_kwh": float(visible.battery_end_kwh.iloc[-1]),
                "full_end_kwh": float(hidden.battery_end_kwh.iloc[-1]),
                "cost_score": float(visible.cost_score.sum() + hidden.cost_score.sum()),
                "protection_score": float(visible.protection_score.sum() + hidden.protection_score.sum()),
                "terminal_score": float(visible.terminal_score.sum() + hidden.terminal_score.sum()),
                "first_step": {name: float(visible.iloc[0][name]) for name in ("battery_end_kwh", "grid_import_kwh", "grid_export_kwh", "solar_clipped_kwh")},
            }
            # Validate port ratings, exclusions and limits in hidden rows too.
            for frame in (visible, hidden):
                f = frame.drop(columns=["starts_at", "ends_at", "tariff_name"]).astype(float)
                hours = np.array([(b-a).total_seconds()/3600 for a,b in zip(frame.starts_at,frame.ends_at)])
                charge = (f.solar_to_battery_kwh + f.grid_to_battery_kwh) / device.charge_efficiency
                discharge = f.battery_to_load_kwh + f.battery_to_grid_kwh
                ac = f.grid_export_kwh - f.grid_import_kwh + f.load_kwh
                assert (charge <= device.inverter_battery_charge_power_kw * hours + 1e-5).all()
                assert (discharge <= min(device.inverter_battery_discharge_power_kw,device.inverter_max_active_power_kw) * hours + 1e-5).all()
                assert (ac <= device.inverter_max_active_power_kw * hours + 1e-5).all()
                assert (ac >= -min(device.inverter_max_absorption_power_kw,device.inverter_max_active_power_kw) * hours - 1e-5).all()
                assert (f.battery_to_grid_kwh <= f.limit_battery_export_rate_to_grid_kw * hours + 1e-5).all()
                assert (f.battery_end_kwh >= f.minimum_battery_kwh - 1e-5).all()
                assert (f.battery_end_kwh <= f.maximum_battery_kwh + 1e-5).all()
                assert not ((f.grid_import_kwh > 1e-5) & (f.grid_export_kwh > 1e-5)).any()
                assert not ((charge > 1e-5) & (discharge > 1e-5)).any()
        full_score = sum(s["probability"] * s["full_score"] for s in scenario_metrics.values())
        full_cost = sum(s["probability"] * s["full_cost"] for s in scenario_metrics.values())
        if model == "lp":
            np.testing.assert_allclose(full_score, metrics["objective"], atol=1e-6, rtol=0)
        else:
            metrics["solver_status"] = "approximate_policy"
        metrics.update(
            total_seconds=perf_counter() - begin, cpu_seconds=process_time() - cpu_begin,
            expected_cost=float(sum(s.probability * result.results[s.name].net_cost.sum() for s in scenarios)),
            next_action=asdict(result.next_action), validation="passed",
            objective=full_score, weighted_full_score=full_score, weighted_full_cost=full_cost,
            scenarios=scenario_metrics, visible_periods=len(steps),
        )
        connection.send({"event": "result", "status": "completed", **metrics})
    except Exception:
        connection.send({"event": "result", "status": "error", "error": traceback.format_exc()})
    finally:
        connection.close()


def _run_case(context, case: str, config_path: str, grid: str = "historical", model: str = "lp", cap_seconds: float = CAP_SECONDS, grid_step_percent: float | None = None, power_divisions: int | None = None, refine_band_percent: float | None = None, refinement_stages: list[tuple[float, int, float]] | None = None, dp_backend: str | None = None) -> dict:
    parent, child = context.Pipe()
    process = context.Process(target=_worker, args=(child, case, config_path, grid, model, grid_step_percent, power_divisions, refine_band_percent, refinement_stages, dp_backend))
    process.start()
    child.close()
    try:
        if not parent.poll(30):
            return {"status": "error", "error": "Worker did not prepare inputs within 30 seconds"}
        message = parent.recv()
        if message["event"] != "ready":
            return message
        deadline = perf_counter() + cap_seconds
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
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.is_alive():
                        process.kill()
        process.join()
        parent.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, default=Path("planning-benchmark.json"))
    parser.add_argument("--grid", choices=("historical", "production", "quarter-hour"), default=None)
    parser.add_argument("--model", choices=("lp", "dp"), default="lp")
    parser.add_argument("--cap-seconds", type=float, default=None)
    parser.add_argument("--cases", nargs="+", choices=(*CASES, "solar_4", "load_4"), default=CASES)
    parser.add_argument("--soc-grid-step-percent", type=float, default=None)
    parser.add_argument("--power-divisions", type=int, default=None)
    parser.add_argument("--refine-band-percent", type=float, default=None)
    parser.add_argument("--refinement-stages", nargs="+", metavar="GRID:DIVISIONS:BAND")
    parser.add_argument("--dp-backend", choices=("python", "numba"), default=None)
    args = parser.parse_args()
    if args.dp_backend is not None and args.model != "dp":
        parser.error("--dp-backend requires --model dp")
    args.grid = args.grid or ("production" if args.model == "dp" else "historical")
    refinement_stages = None
    if args.refinement_stages:
        if args.model != "dp" or args.refine_band_percent is not None:
            parser.error("--refinement-stages requires DP and cannot be combined with --refine-band-percent")
        try:
            refinement_stages = []
            for entry in args.refinement_stages:
                step, divisions, band = entry.split(":")
                stage = (float(step), int(divisions), float(band))
                if not 0 < stage[0] <= 100 or stage[1] <= 0 or not 0 < stage[2] <= 100:
                    raise ValueError
                refinement_stages.append(stage)
        except ValueError:
            parser.error("stages must be GRID:DIVISIONS:BAND with positive values and GRID/BAND at most 100")
    if args.refine_band_percent is not None and (args.model != "dp" or not 0 < args.refine_band_percent <= 100):
        parser.error("--refine-band-percent requires DP and a width between 0 and 100")
    if args.soc_grid_step_percent is not None and not 0 < args.soc_grid_step_percent <= 100:
        parser.error("--soc-grid-step-percent must be between 0 and 100")
    if args.power_divisions is not None and args.power_divisions <= 0:
        parser.error("--power-divisions must be positive")
    cap_seconds = args.cap_seconds if args.cap_seconds is not None else 300. if args.model == "dp" else CAP_SECONDS
    if not 0 < cap_seconds < float("inf"):
        parser.error("--cap-seconds must be finite and positive")
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    report = {
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "cap_seconds": cap_seconds, "grid": args.grid, "model": args.model,
        "soc_grid_step_percent": args.soc_grid_step_percent if args.model == "dp" else None,
        "power_divisions": args.power_divisions if args.model == "dp" else None,
        "refine_band_percent": args.refine_band_percent,
        "refinement_stages": refinement_stages,
        "dp_backend": args.dp_backend,
        "timesteps_minutes": STEPS if args.grid == "historical" else [15] * 96 if args.grid == "quarter-hour" else None,
        "timing_scope": "prepare, solve, extraction and validation including lazy DP Numba import/cache loading/compilation; controller startup excluded" if args.model == "dp" else "build, compile, solve, extraction and validation; controller startup excluded",
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "model_sha256": hashlib.sha256(Path(__file__).parents[1].joinpath(f"planning/{'plan_optimisation_dynamic_programming' if args.model == 'dp' else 'plan_optimisation_milp'}.py").read_bytes()).hexdigest(),
        "scoring_sha256": hashlib.sha256(Path(__file__).parents[1].joinpath("planning/plan_optimisation_dynamic_programming.py" if args.model == "dp" else "planning/plan_optimisation_milp.py").read_bytes()).hexdigest(),
        "system_sha256": hashlib.sha256(Path(__file__).parents[1].joinpath("planning/plan_optimisation_dynamic_programming.py" if args.model == "dp" else "planning/system.py").read_bytes()).hexdigest(),
        "cases": [],
    }
    context = mp.get_context("spawn")
    print(f"{'Case':<20} {'Status':<10} {'Total s':>9} {'Build s':>9} {'Compile s':>10} {'HiGHS s':>9}", flush=True)
    for case in args.cases:
        record = {"case": case, **_run_case(context, case, str(args.config.resolve()), args.grid, args.model, cap_seconds, args.soc_grid_step_percent, args.power_divisions, args.refine_band_percent, refinement_stages, args.dp_backend)}
        record.pop("event", None)
        report["cases"].append(record)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        times = [f"{record[key]:.3f}" if record.get(key) is not None else "N/A"
                 for key in ("total_seconds", "build_seconds", "compile_seconds", "highs_solve_seconds")]
        print(f"{case:<20} {record['status']:<10} {times[0]:>9} {times[1]:>9} {times[2]:>10} {times[3]:>9}", flush=True)
        if record["status"] == "error":
            print(record["error"], flush=True)
    print(f"Saved {len(args.cases)} single-run cases to {args.output}", flush=True)
    if any(record["status"] == "error" for record in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
