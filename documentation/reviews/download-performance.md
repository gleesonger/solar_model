# Analysis download performance

Reviewed and changed on **2026-10-15**. Benchmarks used a temporary SQLite backup of the source database opened read-only. Production data/schema/configuration were not modified.

## Current writer decision

Following the openpyxl comparison below, the user selected **XlsxTurbo** again. Current downloads pass the whole pandas DataFrame to `xlsxturbo.df_to_xlsx`, backed by Rust `rust_xlsxwriter`, with no application row/cell writing loops. Requirements include `xlsxturbo>=1.11,<2`; openpyxl is no longer the dashboard writer. Native SQL aggregation and joins remain in place. The current implementation corresponds to **refinement 2** in the table; refinement 3 is the retained openpyxl comparison. Functional workbook tests cover values, blanks, local dates and infinity text.

## Bottlenecks and refinements

- The original exporter loaded 75,705 electrical and 37,599 forecast ORM objects, decoded fields and built Python dictionaries before pandas grouping. An hourly profile recorded about 23 million calls.
- The first optimization moved grouping into SQLite but retained Python callbacks for periods/last readings/labels, separate queries and a pandas merge. Row-wise XlsxWriter improved workbook generation but still processed every cell in Python.
- The current [SQL exporter](../../solar_model/storage/queries/exports.py) uses one native SQLite statement: timezone-offset lookup, bounded sources, first forecast collection per local day, native sums/means, last-value joins, ordered label concatenation, period joins and readable column aliases. No Python SQL callbacks or pandas aggregation/merge remain. Existing timestamp primary keys support the bounded reads; no migration/index change is required.
- `pandas.read_sql_query` loads only final output rows. Python builds the small timezone-offset lookup and restores final period timezones; it does not process source measurements individually. Excluding forecasts removes forecast-table references entirely.
- [Excel output](../../solar_model/dashboard/components/download_dialog.py) passes the pandas DataFrame directly to **XlsxTurbo**, whose Rust writer avoids project Python row/cell iteration and pandas Excel formatting. CSV continues to use pandas directly. The DataFrame and final file remain in memory; constant-memory workbook mode is not network streaming.
- Calamine is a [pandas read engine](https://pandas.pydata.org/docs/user_guide/io.html), not an XLSX writer. [XlsxTurbo](https://pypi.org/project/xlsxturbo/) accepts pandas DataFrames and byte buffers and uses `rust_xlsxwriter`; requirements include `xlsxturbo>=1.11,<2`. The existing XlsxWriter dependency remains for other scripts.
- Logs separate native SQL/read, data preparation, file generation, output rows/bytes and total preparation time.

## Local measurements by refinement

Single-run, unprofiled observations on the same copied dataset; filesystem cache, machine load and concurrent tests affect results. Original, first-pass and second-pass timings were measured earlier; the third refinement was measured on 2026-10-15 using the same saved database copy and date bounds (2026-08-22 through 2026-10-14, Europe/Dublin). All four current result frames matched the saved original outputs within floating-point tolerance, and each workbook ZIP passed its integrity check. These these are indicative rather than guaranteed speedups. Data and workbook durations are measured separately.

| Export | Original data / XLSX | Refinement 1: SQL + XlsxWriter | Refinement 2: native SQL + Rust (current) | Refinement 3: native SQL + pandas/openpyxl (comparison) |
| --- | --- | --- | --- | --- |
| Hourly, no forecasts | 6.21s / 1.48s | 1.07s / 0.49s | 0.44s / 0.05s | 0.50s / 1.62s |
| Hourly, forecasts | 7.10s / 1.96s | 1.15s / 0.67s | 0.42s / 0.07s | 0.63s / 1.35s |
| Daily, forecasts | 14.83s / 0.16s | 1.80s / 0.09s | 0.37s / 0.01s | 0.45s / 0.07s |
| Minute, forecasts | 24.45s / 111.96s | 8.88s / 55.67s | 2.83s / 3.30s | 3.96s / 67.58s |

Totals below use underlying unrounded stage measurements where available:

| Request (with forecasts) | Original | Refinement 1 | Refinement 2 (current) | Refinement 3 (comparison) |
| --- | --- | --- | --- | --- |
| Small: daily, 54 rows | 14.99s | 1.89s | 0.38s | **0.52s** |
| Medium: hourly, 1,284 rows | 9.06s | 1.82s | 0.49s | **1.99s** |
| Large: minute, 75,725 rows | 136.41s | 64.55s | 6.13s | **71.54s** |

All forecast-enabled outputs have 83 columns. The measured openpyxl minute workbook was approximately 23.7 MB; the current Rust-writer benchmark produced approximately 18.5 MB. Openpyxl workbook generation accounts for 67.58s of its 71.54s preparation time; using one pandas API call still entails internal cell serialization. Browser transfer time is excluded. First calls may include writer import/setup costs; these measurements are single runs rather than warmed repeated trials. Per-stage metrics are saved alongside the temporary benchmark database in `openpyxl-metrics.json`.

## Correctness checks

- Compared all columns of hourly (with/without forecasts), daily and minute exports with saved original outputs on the real-data copy. Periods, labels, forecast vintages and numeric values matched within floating-point tolerance; null representations can differ (`None` versus `NaN`).
- Regression comparisons cover seven timesteps, Dublin spring/autumn DST, a half-hour-offset timezone, last-versus-max counters, independent null patterns, ordered tariff labels, forecast vintages, empty/one-sided ranges and forecast skipping. Query checks require one SQL statement with or without forecasts.
- Workbook tests inspect numeric values, local datetime serials, blank cells, labels and infinity text. Formatted empty cells may be explicitly present in workbook XML and remain blank.
- Validation: `python -m pytest -q --ignore=tests/test_database_0_1_0_migration.py --disable-warnings` returned **137 passed, 1 skipped**. The pre-existing missing `migrate_database_0_1_0` import still blocks full-suite collection; this is the documented subset, not a full-suite pass.

See [dashboard data processing](../implementation/dashboard-data.md) and [presentation/export handling](../implementation/dashboard-ui.md). Older ORM/pandas helpers remain for compatibility/regression comparisons, not current downloads.
