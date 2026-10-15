# Recent summary performance

Implemented and verified on **2026-10-15**, following a native SQLite feasibility investigation. Source database access was read-only, followed by SQLite backup to temporary databases. Production data/schema/configuration were not modified.

## Current implementation

[summary_data_sql.py](../../dashboard/summary_data_sql.py) performs the daily, monthly and lifetime numeric calculations in native SQLite. [snapshot.py](../../dashboard/snapshot.py) calls `load_recent_energy_summaries()` once for both Recent summary tables. Two indexed minimum-timestamp queries establish the lifetime start, followed by one shared aggregation statement. No schema migration or new index is required.

- A small calendar CTE supplies configured-local dates and UTC midnight boundaries. Indexed range joins select hourly actuals and the first forecast collection per date.
- Only the eight required energy/economics columns are summed from hourly rollups, explicitly converting fixed-point storage with `/10000.0`.
- SQL unpivots configured panels' non-null adjusted cumulative Wh. `LAG` partitions by collection date, panel and target local date. First values contribute themselves; decreases clamp to zero; `/10000000.0` converts storage to kWh.
- The export helper's compact timezone-offset lookup provides native target-local dates without Python callbacks per measurement. The lookup only needs the eligible calendar range plus the following date; a left join preserves the existence of targets outside that range.
- SQL retains the existing 24 elapsed-hour forecast labels and today's partial-hour weighting. No configured non-null points remains missing; points solely outside eligible hours produce zero. Existing broader DST design questions remain in [findings](findings.md).
- Daily history is materialized once. SQL returns seven descending dates plus Total, all twelve descending calendar months including empty months, their Total, and Lifetime with its separate date predicate. Python only prepares calendar/offset parameters and separates/formats the final small tables.
- Standalone daily/monthly loaders use the same engine. `load_daily_energy_totals()` requests daily rows without totals. The Live/Today loader remains separate.

## Integrated before/after measurements

Three alternating unprofiled runs used the same database copy and frozen current time, comparing the previous daily-plus-monthly loaders with the integrated implementation. Both versions ran in the repository virtual environment. Timings include lifetime queries, calendar/offset preparation, SQL, pandas read and final formatting.

| Both Recent summary tables | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| Previous ORM/pandas implementation | 1.507s | 2.006s | 2.050s |
| Shared native SQL implementation | 0.074s | 0.064s | 0.064s |

All columns and rows of both tables matched the previous implementation within `rtol=atol=1e-9`, including twelve-month padding, labels, order, Total and Lifetime. Equivalent null representations (`None` and `NaN`) were normalized for comparison. Output sizes were 8 daily rows and 14 monthly rows; bounds ended on 2026-10-15. These are local observations, not full-dashboard timings or guaranteed deployment speedups. Machine load, filesystem cache and concurrent tests affect timings. Multi-year production performance was not benchmarked.

## Initial investigation

The investigation copy contained 1,290 hourly rows and 38,145 forecast rows. A cProfile run of the old monthly loader recorded **3.21 million calls** and **404 calls to `aggregate_forecast()`**: 349 calendar days in the twelve-month request plus 55 lifetime days. The database history began on 2026-08-22; absent days still built empty 24-hour pandas frames. Forecast aggregation accounted for **3.404s of 4.211s profiled elapsed time**; ORM result loading took approximately 0.56s cumulative. Profiler overhead means these numbers should not be compared directly to unprofiled timings.

The old monthly loader alone measured 2.234s, 2.328s and 2.215s. An initial 55-day native daily prototype measured 0.045s, 0.034s and 0.043s including preparation and pandas read. Its monthly grouping measured 0.027-0.028s excluding preparation, but omitted empty-month padding and the separate seven-day table. The integrated measurements above replace those narrower feasibility timings for assessing the completed change.

`EXPLAIN QUERY PLAN` on the prototype showed indexed hourly time-range searches, forecast collection-range searches and composite collection/target searches. SQLite uses temporary B-trees for grouping/window sorting. The implementation retains indexed range joins, but no per-row Python SQL callbacks or ORM object materialization.

## Validation

- [test_recent_summary_sql.py](../../tests/test_recent_summary_sql.py): 17 cases covering an independent retained ORM/Python forecast reference across Dublin spring/autumn DST and Kolkata, one/two/no configured panels, null readings, decreasing/negative cumulative values, first-vintage selection, out-of-range targets, tariff-split hours, empty databases, twelve empty-month rows, leap-day history exceeding twelve months, distinct totals, shared snapshot loading and invalid ranges.
- Query instrumentation requires two lifetime-minimum statements and exactly one shared aggregation statement for both tables. Standalone daily/monthly outputs match the combined results.
- Real-data-copy comparison checked every row and column against the previous loaders with time frozen for identical elapsed-day forecast weights.
- Full suite: `.venv/Scripts/python.exe -m pytest -q` returned **255 passed, 1 skipped**, including the legacy-named migration regression. Warnings are dependency/deprecation notices. An initial system-Python attempt crashed importing its OSQP extension; the repository virtual environment completed the full suite.
- `git diff --check` passed. No visual styling changed.

The two temporary investigation scripts and their generated Python bytecode were deleted at the user's request. Production uses the repository module and regression tests; no temporary script is a runtime dependency.
