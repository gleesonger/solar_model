# Isolated planning setup experiments

Measured on 2026-10-27 on the remote Intel N150 (four logical CPUs, approximately 16 GB RAM), using Python 3.12.3, CVXPY 1.9.3, HiGHS 1.15.1, NumPy 2.5.2 and pandas 2.3.3. These are independent experimental copies, not changes applied to application code.

## Method

Every variant starts from the same current working-copy `planning/optimisation.py`, SHA256 `600e8c540a22f36221c6ca3a411e31555b1d6f21de2f0b072be7cf61f6b970cd`. Changes were not layered. Remote variant hashes matched the local experimental copies, and the copied baseline modules matched the application files after measurement.

Each call solves one joint nine-scenario problem over 24 hours, using `[15] * 12 + [30] * 6 + [60] * 18`: 36 periods per scenario. The 10:05 UTC start, synthetic solar/house-load forecasts, configured tariffs/bounds, X = 5, 18 kWh battery starting at 8 kWh, 5 kW power limits and 95% efficiencies match the preceding remote benchmark. The grid override is common to all variants; it does not change the application's projection settings.

Each variant runs in its own Python process: one initial simulation, followed by three simulations whose arithmetic average is the reported result. Inputs are identical between calls. Imports, forecast/scenario preparation, and post-simulation validation are excluded from simulation timing. Model construction/input updates, compilation, native solving and result extraction are included. HiGHS options stay automatic (`threads=0`, `parallel=choose`); unrelated BLAS/OpenMP pools are capped at one consistently. Solver warm starts are disabled for every variant, so parameter reuse measures construction/compilation reuse rather than reusing a previous solution.

An in-process watchdog and external controller enforce five seconds per simulation. A timeout would kill that variant and skip its remaining calls while continuing the other variants. No simulation reached the limit.

## Results

All times are seconds. Setup is construction/input update plus CVXPY compilation. The initial call is shown separately and is not included in the three-call averages. Individual phase averages need not sum exactly to total because total includes additional solver-interface work.

| Independent variant | Initial total | Average setup | Average native solve | Average total | Total reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Unchanged baseline | 1.506 | 0.364 | 0.997 | 1.449 | - |
| Protection matrix across scenarios | 1.265 | 0.193 | 0.998 | 1.247 | 13.9% |
| Construct DataFrames after solving | 1.439 | 0.290 | 1.013 | 1.394 | 3.7% |
| Reuse shared constants | 1.543 | 0.361 | 1.023 | 1.470 | -1.5% |
| Reuse parameterised compiled model | 1.775 | 0.078 | 0.992 | 1.120 | 22.7% |

The constant-cache result is too small to establish a useful improvement; most of the total-time difference is native-solver variation. These measurements concern this synthetic workload and three measured calls, not a worst-case or statistically comprehensive runtime guarantee.

## What each copy changes

- **Protection matrix:** replaces the nine scenario protection vectors/constraint groups with one scenario-by-time matrix. Every supporting-line inequality, score coefficient and scalar decision remains. CVXPY constraint objects drop from 329 to 73; scalar variables remain 3,578 and Booleans remain 332. Average compilation drops from 0.246 s to 0.102 s. The separate constant-caching and deferred-DataFrame changes are not included.
- **Deferred DataFrames:** constructs input records and numeric arrays first, then creates result DataFrames after solving. Baseline scoring and constraint construction remain. Average construction drops from 0.119 s to 0.043 s, while result extraction rises from 0.046 s to 0.051 s. Compilation and native solving remain essentially unchanged.
- **Shared constants:** caches protection segments and calculates hardware limits once per distinct duration rather than once per period. Tariff lookups were already shared across scenarios in the baseline. No other variant is included. Average setup changes only from 0.364 s to 0.361 s.
- **Compiled reuse:** keeps baseline DataFrame construction/scenario protection formulation but represents input matrices, initial battery energy, the SOC big-M coefficient and protection reward with 2,271 scalar CVXPY parameters. The first call builds a DPP-compliant model and stores a closure containing the problem and variables. Later calls update input values and reuse compilation. Average compilation drops from 0.246 s to 0.0016 s. Cache reuse requires the same device/configuration, scenario names/probabilities and interval-duration sequence; incompatible structures rebuild. This has a higher initial cost, so it helps repeat calls rather than one-shot CLI invocations. Changed forecasts, state, interval-start schedule values and reward have update paths, but this timing experiment uses identical inputs on each call.

## Validation and retained files

Twenty completed simulations (four per baseline/variant) returned optimal status, passed independent solar/load/battery balance, scheduled bounds, duration, exclusivity and cost checks, matched the baseline objective within 1e-5, and produced exactly the same `NextAction`. Reordering protection constraints changed the reported objective by approximately 8.49e-7, within the baseline solver gap. No protection approximation, objective weight, physical limit or MIP tolerance was relaxed.

A summary-print field-name error occurred after the first four experiments had already saved all four valid runs to JSON. Their measurements were retained without rerunning simulations. Parameter-shape/elementwise-product errors in the initial reuse prototype occurred before solving; those were corrected before its four successful measurements. No timed-out experiment was retried.

Remote reproduction directory: `/tmp/solar-plan-benchmark-RPyg8INp/isolated-it`. It contains:

- `variants/baseline.py`, `protection_matrix.py`, `deferred_dataframes.py`, `shared_constants.py`, `compiled_reuse.py`: independent source copies.
- `make_variants.py`: transformations from the saved baseline.
- `benchmark_it.py`: common four-call benchmark with a five-second watchdog.
- `results-it-<variant>.json`, `log-it-<variant>.txt`: raw measurements and logs.
- `it-comparison-final.json`: validated combined results, including the three-call arithmetic averages and source hashes.

From that directory, an individual experiment can be run with `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 ../.venv/bin/python benchmark_it.py protection_matrix` (substitute another variant name as required). The common benchmark retains its in-process five-second watchdog; the original batch additionally used a parent-process deadline.

## Six-hour fine-grid follow-up

On 2026-10-27, compared only baseline and compiled reuse on a nominal six-hour quarter-hour section followed by hourly periods to 24 hours. The first 15-minute action remains intact; subsequent fine periods align to whole UTC hours, dropping surplus fine-prefix periods when needed. At the same 10:05 UTC start, the actual duration vector is `[15, 10, 15, 15] + [15] * 20 + [60] * 18 + [5]`: 43 periods per scenario, with the fine section ending at 16:00 UTC and the complete projection ending at 10:05 UTC the next day. The shorter alignment interval is deliberate. Both variants solve all nine scenarios jointly with 4,271 scalar variables and 395 Booleans. No other experimental optimisation is included.

The environment, inputs, automatic HiGHS settings, disabled solver warm starts and separate-process methodology are unchanged. Each variant ran once initially, then three measured calls. Both a watchdog and external controller enforced five seconds per simulation; no timeout occurred.

| Measurement | Baseline | Compiled reuse |
| --- | ---: | ---: |
| Initial total | 1.042 s | 1.349 s |
| Mean of three subsequent totals | 1.010 s | 0.674 s |
| Mean construction/input update | 0.134 s | 0.089 s |
| Mean compilation | 0.251 s | 0.0017 s |
| Mean native solve | 0.537 s | 0.533 s |
| Mean result extraction | 0.045 s | 0.041 s |
| Process peak RSS across calls | 235.8 MiB | 240.7 MiB |

Repeat-call total time decreased by 33.3%, with native solve time essentially unchanged. The first reusable-model call remained slower because it constructs and compiles the parameterised problem. All eight calls returned optimal status, passed physical/cost/duration checks, matched objective within 1e-5 and returned identical next actions. The three measured totals were 0.997, 1.014 and 1.018 s baseline, versus 0.670, 0.684 and 0.667 s with reuse.

The grid was overridden only in `benchmark_reuse_6h.py`; application code remains unchanged. The same remote experiment directory retains `results-6h-baseline.json`, `results-6h-compiled_reuse.json`, `compiled-reuse-6h-comparison.json` and `log-6h-<variant>.txt`. Reproduce with the earlier environment prefixes and `../.venv/bin/python benchmark_reuse_6h.py baseline` or `compiled_reuse`. None of the other three optimisation variants were run in this follow-up.

The subsequent [alignment investigation](planning-alignment-investigation.md) explains the unusually fast native solve: this 43-period instance avoided an auxiliary integer solve. Pure clock-shift controls did not change the sampled tariffs or schedules. Removing its final five-minute tail made the resulting 42-period instance slower in that workload; the original timings above remain valid for the original 43-period problem.
