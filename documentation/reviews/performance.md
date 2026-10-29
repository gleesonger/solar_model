# Performance review conclusions

Historical investigations, 2026-10-15-27; condensed 2026-10-29. Measurements used copied/synthetic inputs and are not deployment guarantees or benchmarks of the latest working tree. Detailed logs, intermediate variants and generated evidence were removed.

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

- Explicit battery states and matrix constraints reduced a synthetic nine-scenario/33-period case from 5.696 to 1.199 s; compilation fell from 4.120 to 0.258 s. Objective, cost and next action matched checked baselines. Fine-resolution MIPs remain expensive.
- Independent Intel N150 setup experiments on 36 periods averaged 1.449 s baseline, 1.247 s with a protection matrix, 1.394 s with deferred DataFrames and 1.470 s with shared constants. Compiled reuse averaged 1.120 s with higher initial cost. It was subsequently removed because changing projection durations make reuse unsuitable for intended replanning calls. Experimental variants are not all application features.
- Controlled clock shifts with fixed forecasts produced identical solver models and runtimes. A fast 43-period instance avoided a HiGHS auxiliary integer solve; removing its five-minute tail made 42 periods slower (native 0.530 -> 1.187 s). Fewer periods or aligned timestamps do not guarantee faster solving.
- Nineteen fresh-process 43-period/nine-scenario sensitivity cases all validated within five seconds: complete jobs 0.939-1.954 s, native solving 0.414-1.429 s. A separate one-second experiment completed only preference 0; eighteen killed cases remain N/A, even where native logs reached optimality. Single observations do not establish rankings or worst-case bounds.

Run current experiments with `python -m solar_model.tools.benchmark_planning --output planning-benchmark.json`; see the [current timing/input contract](../implementation/planning.md). The tool has evolved and need not reproduce the historical grid. Kill simulations exceeding five seconds, skip remaining runs for that case and continue other cases; report timeouts as N/A unless the user specifies another limit. Distinguish construction, compilation, native solving and validated total runtime.
