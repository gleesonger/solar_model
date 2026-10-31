# Performance review conclusions

The [2026-10-31 DP Numba measurement](dp-numba-performance.md) compares the same numerical core in Python and compiled execution across the complete configured four-stage, nine-scenario solve. Median simulation time over three runs was 15.329 seconds versus 0.575 seconds (26.6 times faster), excluding backend initialisation/compilation. Reported scores, costs, battery endpoints and first commands matched. Warm-up was subsequently removed; current first-call timings include lazy startup. This is separate from the historical investigations below.

Historical investigations, 2026-10-15-27; condensed 2026-10-29. Measurements used copied/synthetic inputs and are not deployment guarantees or benchmarks of the latest working tree. Detailed logs, intermediate variants and generated evidence were removed.

The [2026-10-29 DP versus LP experiment](planning-dynamic-programming-comparison.md) is a separate planning comparison: the seven-action DP completed eight cases in 0.61–1.66 seconds including validation. Four completed configured-grid pairs were 1.62–2.88 times faster than LP, with expected 48-hour net costs EUR 0.060–0.101 higher and combined scores 1.20–1.83% lower. All three 96-step LP runs exceeded five seconds; DP completed in 1.60–1.66 seconds, so their LP economic comparison is N/A. DP had the user-requested 300-second deadline. These are single synthetic observations; DP remains experimental and LP remains the application default.

## Downloads

Native SQLite aggregation replaced ORM object loading and Python grouping; only final rows enter pandas. Dashboard XLSX output uses XlsxTurbo's Rust writer; XlsxWriter remains for report workbooks.

| Forecast-enabled export | Original total | Native SQL + XlsxTurbo |
| --- | ---: | ---: |
| Daily, 54 rows | 14.99 s | 0.38 s |
| Hourly, 1,284 rows | 9.06 s | 0.49 s |
| Minute, 75,725 rows | 136.41 s | 6.13 s |

Single-run copied-data timings exclude browser transfer. The openpyxl minute comparison took 71.54 s. Regressions cover values, vintages, nulls, tariff labels, timesteps, DST and workbook content. DataFrame/file buffers remain in memory. See [export contracts](../implementation/dashboard-data.md) and [download tests](../../tests/dashboard/test_analysis_download.py).

## Recent summaries

Daily/monthly/lifetime summaries share one native SQL aggregation after two minimum-timestamp queries. Three alternating copied-data runs improved both tables from 1.507-2.050 s to 0.064-0.074 s with matching rows/values and twelve-month padding. Live/Today remains separate; multi-year production performance was not measured. See [summary tests](../../tests/dashboard/test_recent_summary_sql.py).

## Forecast preparation

SQL hourly means, narrow forecast projections and selection of the 56-day scalar training window before merging reduced preparation costs while retaining distribution history:

| Synthetic history | Old load + target-day calculation | New load + calculation | Old -> new peak working set |
| --- | ---: | ---: | ---: |
| One year | 2.53 + 15.72 s | 0.94 + 0.16 s | 394.5 -> 195.1 MiB |
| Three years | 9.03 + 135.33 s | 2.89 + 0.33 s | 1047.2 -> 451.6 MiB |

Fresh-process local single runs exclude network, writes, Wh reconstruction and additional target days. Scalar/prediction parity was checked; retained forecast history still grows with database age.

## Planning

The retained [Python-list mode-mask model](planning-mode-mask-comparison.md#python-list-follow-up-retained) has no direct NumPy imports in the optimiser or physical/report helper. Inputs and report columns are lists; physical equations use per-period scalar limits and one-dimensional scenario lists passed directly to CVXPY. All 21 production inputs completed in 1.209-2.834 seconds, median 2.092 seconds versus 1.830 for the preceding array-based mode-mask version; median paired slowdown was about 10%. All nineteen finer-grid inputs also completed, including the two previous timeouts, in 1.540-4.535 seconds. Weighted full-cost differences remained below EUR 0.003111. These single-run results describe the Python-list snapshot before the subsequent command/response limit correction, not the current formulas. No fresh performance/economic comparison has been run for that correction; the following investigations also describe historical snapshots.

The historical [fixed-mode worker trial](planning-fixed-mode-comparison.md) simplified first-step mode equations and preserved completed-case economics, but fresh three-process startup on every call regressed production completion from 21/21 to 17/21 and finer-grid completion from 16/19 to 2/19 within five seconds. Production median completed time increased from 2.034 to 4.505 seconds; median paired slowdown was 2.25 times. That implementation was replaced by the single-solve model. Reusable prewarmed workers were not pursued.

The earlier [2026-10-29 single-solve port-model comparison](planning-port-comparison.md) compared that rewrite with the route model using identical synthetic inputs and nine scenarios per solve. All 21 production-grid inputs completed in 0.779-3.637 s; the median paired slowdown was about 14%, with maximum weighted full-cost difference EUR 0.002636 across completed pairs. On the finer 43-period grid, three of nineteen port-model inputs exceeded five seconds. Those remain N/A. Zero protection preference also produced different visible-horizon schedules with identical full-horizon economics; see the comparison for details.

- Explicit battery states and matrix constraints reduced a synthetic nine-scenario/33-period case from 5.696 to 1.199 s; compilation fell from 4.120 to 0.258 s. Objective, cost and next action matched checked baselines. Fine-resolution MIPs remain expensive.
- Independent Intel N150 setup experiments on 36 periods averaged 1.449 s baseline, 1.247 s with a protection matrix, 1.394 s with deferred DataFrames and 1.470 s with shared constants. Compiled reuse averaged 1.120 s with higher initial cost. It was subsequently removed because changing projection durations make reuse unsuitable for intended replanning calls. Experimental variants are not all application features.
- Controlled clock shifts with fixed forecasts produced identical solver models and runtimes. A fast 43-period instance avoided a HiGHS auxiliary integer solve; removing its five-minute tail made 42 periods slower (native 0.530 -> 1.187 s). Fewer periods or aligned timestamps do not guarantee faster solving.
- Nineteen fresh-process 43-period/nine-scenario sensitivity cases all validated within five seconds: complete jobs 0.939-1.954 s, native solving 0.414-1.429 s. A separate one-second experiment completed only preference 0; eighteen killed cases remain N/A, even where native logs reached optimality. Single observations do not establish rankings or worst-case bounds.

Run current experiments with `python -m solar_model.tools.benchmark_planning --output planning-benchmark.json`; see the [current timing/input contract](../implementation/planning.md). The tool has evolved and need not reproduce the historical grid. Kill simulations exceeding five seconds, skip remaining runs for that case and continue other cases; report timeouts as N/A unless the user specifies another limit. Distinguish construction, compilation, native solving and validated total runtime.
