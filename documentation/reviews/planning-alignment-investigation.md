# Planning alignment and solver runtime investigation

Investigated on 2026-10-27 on the remote Intel N150, using the same synthetic forecasts, nine joint scenarios, tariffs, battery/inverter settings and Python/HiGHS environment as the [planning setup experiments](planning-it-performance.md). No compiled-model reuse or other experimental setup optimisation was enabled.

## Findings

The large earlier runtime improvement was not evidence that clock-aligned tariff processing is faster. In the controlled pairs below, the sampled tariff and scheduled battery-limit values were identical. Holding forecast energy fixed made the complete solver problems identical too, including all objective coefficients, matrix entries, row/column bounds and integer-variable declarations. Those pairs had identical LP iteration counts and essentially identical solve times.

The previously fast 43-period instance took a cheaper path through HiGHS's integer-feasibility heuristics. Its log reports no auxiliary integer solve (sub-MIP), while the original 36-period instance spent approximately 0.43 seconds solving a sub-MIP, plus its additional presolve. This accounts for most of the approximately 0.46-second native-solver gap. Both instances report one main search node; that count does not reveal the auxiliary solves. The original inference that one node implied little integer-search work was incomplete.

Removing the five-minute tail from that exact 43-period instance brought back an auxiliary solve: the resulting 42-period instance averaged 1.187 seconds native solving, compared with 0.530 seconds with the tail. Its smaller size did not make it faster. This is a reproducible change in the solver's heuristic path for this workload, not a general reason to retain an otherwise unwanted tail.

## Method and controls

Each case used one initial call followed by three measured calls; results below are arithmetic averages of the three measured calls. Each case ran in a fresh process. Solver options stayed automatic (`threads=0`, `parallel=choose`), solver warm starts were disabled and unrelated BLAS/OpenMP pools were capped at one. Diagnostics recorded canonical HiGHS model hashes and native solver logs. Every simulation had both an in-process five-second watchdog and an external controller deadline; a timeout would kill that case and skip its remaining calls. All 32 calls completed below the limit and passed independent energy, cost, battery-bound, duration and charge/discharge-exclusivity checks.

The underlying forecast is the same UTC half-hour data in every case. For the fixed-forecast controls only, a forecast wrapper queries each aligned interval five minutes later. This reproduces the exact original per-row forecast energy while changing displayed/query grid timestamps. No device state, tariffs, efficiencies, scenario probabilities or protection approximation were changed.

The controls use complete 24-hour elapsed windows unless marked trimmed, to reproduce the original comparison. They override the grid only in the experiment; the application now uses the full-hour endpoint policy.

## Controlled results

| Case | Periods/scenario | Mean native solve | Mean full simulation | LP iterations | Mean auxiliary solve |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original 3h quarter-hour / 3h half-hour grid, starts 10:05 | 36 | 0.993 s | 1.450 s | 2,759 | 0.433 s |
| Same grid starts 10:00, original per-row forecast energy held fixed | 36 | 1.004 s | 1.463 s | 2,759 | 0.437 s |
| Same grid starts 10:00, forecasts integrated over actual intervals | 36 | 0.953 s | 1.410 s | 2,601 | 0.253 s |
| Six-hour quarter-hour grid starts 10:05, hourly remainder | 42 | 0.871 s | 1.352 s | 3,143 | 0.327 s |
| Same grid starts 10:00, original per-row forecast energy held fixed | 42 | 0.867 s | 1.346 s | 3,143 | 0.323 s |
| Same grid starts 10:00, forecasts integrated over actual intervals | 42 | 1.117 s | 1.598 s | 3,425 | 0.130 s |
| Original fast aligned grid starts 10:05, including final five minutes | 43 | 0.530 s | 1.008 s | 2,894 | 0.000 s |
| Same aligned grid, final five minutes removed | 42 | 1.187 s | 1.677 s | 3,060 | 0.433 s |

The auxiliary-solve column is parsed from the native logs' sub-MIP solve-time breakdown; it excludes auxiliary presolve and uses the logs' two-decimal timing precision. Cases without auxiliary solves report zero. Every completed call reported one main search node. LP iteration counts were stable across all four calls of each case.

### Model equality checks

For both the 36-period and 42-period pairs:

| Component compared against original offset grid | Aligned, fixed forecast | Aligned, actual forecast |
| --- | --- | --- |
| Sampled import/export tariffs and battery-limit schedules | Identical | Identical |
| Per-row solar/load energy | Identical | Different |
| Solver matrix | Identical | Identical |
| Objective coefficients/offset | Identical | Identical |
| Solver column bounds and integrality | Identical | Identical |
| Solver row bounds | Identical | Different |

The actual-forecast pair changes forecast right-hand-side values through correct overlap integration, not through rounding or dropping readings. Moving the grid five minutes changes the energy assigned to each period. Alignment made the 36-period example slightly faster but the 42-period example slower; there is no consistent alignment speed advantage in these controls.

### Native solver evidence

The original 36-period instance produced its final incumbent on an `L` row, identified in the HiGHS log as a sub-MIP solution. A representative run records 0.50 seconds main-MIP solve work and 0.44 seconds sub-MIP solve work, with maximum sub-MIP depth 2.

The 43-period instance produced its final incumbent on an `H` heuristic row, with matching primal and dual bounds. It records approximately 0.49 seconds solve time, no sub-MIP timing and maximum sub-MIP depth 0.

The trimmed 42-period instance again produced its incumbent on an `L` row. A representative run records 0.68 seconds main-MIP solve work and 0.45 seconds sub-MIP solve work, with maximum sub-MIP depth 2. Its different endpoint also changes the feasible terminal trajectory and removes the tail's cost/protection contribution; it is not merely a display change.

These logs locate the extra time in the solver. They do not prove which individual forecast number or coefficient caused its heuristic decision. The controlled equality checks do rule out tariff/schedule lookup overhead and clock timestamps themselves as the explanation for the earlier large speed-up.

## Retained reproduction files

Remote directory: `/tmp/solar-plan-benchmark-RPyg8INp/alignment-investigation`.

- `diagnose_alignment.py`: common baseline-only model, grid/forecast controls and canonical model hashing.
- `run_investigation.py`: all eight cases, four calls per case and external five-second deadline.
- `results-<case>.json`, `controller-<case>.log`: timings, input hashes and validation output.
- `highs-<case>-<repeat>.log`: native solver traces, including auxiliary solves.
- `alignment-comparison-validated.json`: combined results and parsed mean auxiliary-solve times.

Run the controller with the existing environment: `/tmp/solar-plan-benchmark-RPyg8INp/.venv/bin/python run_investigation.py`. It runs no 96-period cases. The five-second performance-experiment rule is now recorded in the repository's `AGENTS.md` for future investigations.

## One-second input-sensitivity follow-up

On 2026-10-27, ran nineteen fresh-process, single-simulation cases on the original 43-period grid, including its five-minute tail. Compiled reuse was disabled and no case was repeated. All cases retained the nine scenarios, 10:05 UTC start, 18 kWh capacity, 5 kW limits, efficiencies, scenario probabilities and automatic HiGHS settings. Each case changed only the stated input from the baseline (8 kWh starting energy, solar/load multipliers 1, preference 5 and reserve 4 kWh).

| Changed input | Values tested | Completed within deadline |
| --- | --- | --- |
| None | Baseline | N/A |
| Initial battery energy | 4, 7.9, 8.1, 14 kWh | N/A for all four |
| Baseline solar forecast multiplier | 0.75, 0.95, 1.05, 1.25 | N/A for all four |
| Baseline house-load forecast multiplier | 0.75, 0.95, 1.05, 1.25 | N/A for all four |
| Protection preference | 0, 50 | Preference 0 completed; 50 was N/A |
| Preferred reserve | 2, 6 kWh | N/A for both |
| Import tariff | Flat at baseline 24-hour mean; peak band rate multiplied by 1.5 | N/A for both |

The preference-0 case returned an optimal plan in 0.929 seconds: construction 0.137 s, compilation 0.288 s and native solving 0.415 s, with physical/cost/duration checks passed. The other eighteen processes were killed at the one-second limit and were not retried. Their eventual completion times are unknown; N/A is not evidence of infeasibility or a substantially longer solve.

The one-second cap covered the complete uncached job, not one second of native solver time. Imports and synthetic-input preparation occurred before timing, but construction, compilation, solving and output processing consumed the budget. The external controller remained armed through post-simulation validation/output, which is slightly stricter than the stored `total_seconds` field (that field stops when `simulate_energy_plan` returns). Two killed cases, initial battery 8.1 and 14 kWh, had already reached native optimal status according to the logs (approximately 0.53 and 0.45 seconds of native solving respectively), but did not return a completed validated benchmark record before the external deadline. They remain N/A. No additional simulation was run to recover those measurements.

This small deadline, with the baseline itself at the threshold, censors most results and does not measure the full distribution of input-dependent completion times. It establishes that the tested uncached pipeline did not consistently return validated output inside one second, not that all the changed inputs require more than one second inside HiGHS.

Retained remote directory: `/tmp/solar-plan-benchmark-RPyg8INp/input-sensitivity`. `input_sensitivity.py` defines the cases and one-second watchdog; `run_sensitivity.py` enforces the independent one-second process deadline and exactly one run per case. The directory also contains `results-<case>.json`, `highs-<case>-1.log`, `controller-<case>.log` and `input-sensitivity-validated.json`, including the audit of native logs for killed cases. Application code was not modified.

## Five-second input-sensitivity follow-up: single script

Repeated all nineteen cases once on the same remote Intel N150, using the same inputs and original 43-period grid. The new repository tool [benchmark_planning.py](../../solar_model/tools/benchmark_planning.py) contains both the case loop and worker implementation. Each case uses a fresh process and uncached model, with warm starts disabled. A parent-controlled five-second deadline kills and reaps overdue workers and continues without retrying. Imports/input preparation are excluded; the budget includes simulation and validation. This is a stricter budget than five seconds of native solving alone.

All nineteen cases returned optimal results and passed battery/solar/load conservation, cost, battery bounds, charge/discharge exclusivity and interval-duration checks. No case reached the cap. The remote configuration and optimisation-source SHA-256 hashes match the local working files. Times below are seconds; total includes validation (approximately 0.027 seconds), while simulation stops when `simulate_energy_plan` returns.

| Case | Simulation | Total including validation | HiGHS solve |
| --- | ---: | ---: | ---: |
| Baseline | 1.050 | 1.078 | 0.539 |
| Initial battery 4 kWh | 1.491 | 1.518 | 0.991 |
| Initial battery 7.9 kWh | 1.478 | 1.505 | 0.978 |
| Initial battery 8.1 kWh | 1.028 | 1.055 | 0.527 |
| Initial battery 14 kWh | 0.952 | 0.979 | 0.454 |
| Solar ×0.75 | 1.555 | 1.582 | 1.051 |
| Solar ×0.95 | 1.481 | 1.508 | 0.980 |
| Solar ×1.05 | 1.237 | 1.264 | 0.737 |
| Solar ×1.25 | 1.653 | 1.680 | 1.150 |
| House load ×0.75 | 1.208 | 1.236 | 0.710 |
| House load ×0.95 | 1.065 | 1.096 | 0.552 |
| House load ×1.05 | 1.421 | 1.448 | 0.924 |
| House load ×1.25 | 1.926 | 1.954 | 1.429 |
| Protection preference 0 | 0.912 | 0.939 | 0.414 |
| Protection preference 50 | 1.575 | 1.602 | 1.073 |
| Reserve 2 kWh | 1.262 | 1.290 | 0.721 |
| Reserve 6 kWh | 1.469 | 1.497 | 1.059 |
| Flat import tariff | 1.378 | 1.405 | 0.876 |
| Peak import tariff ×1.5 | 1.542 | 1.569 | 1.036 |

Observed native solve times vary from 0.414 to 1.429 seconds (approximately 3.45×), with complete validated jobs between 0.939 and 1.954 seconds (approximately 2.08×). Construction stays around 0.13–0.14 seconds and compilation around 0.23–0.34 seconds. These single observations show input sensitivity; they do not establish stable rankings or a worst-case bound for other inputs.

Run from the repository root:

```sh
python -m solar_model.tools.benchmark_planning --output planning-benchmark.json
```

Remote invocation used `/tmp/solar-plan-benchmark-RPyg8INp/.venv/bin/python -u -m solar_model.tools.benchmark_planning --output results-5s.json` from `/tmp/solar-plan-benchmark-RPyg8INp/input-sensitivity`. Only the one new Python tool was copied for this rerun; it produces a single JSON report, updated after each case. The report is retained remotely as `results-5s.json` and locally at `C:\Users\glees\AppData\Local\Temp\solar_input_sensitivity_xitu81sv\results-5s.json`.
