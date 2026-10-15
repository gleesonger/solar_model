# Development and testing

## Setup

Run from the repository root. Docker uses Python 3.12.10; the reviewed local virtual environment uses Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

- [requirements.txt](../../requirements.txt) contains runtime ranges rather than a lockfile. Pytest, Playwright, and extraction's openpyxl are not listed.
- Planning explicitly requests HiGHS. Verify with `python -c "import cvxpy; print(cvxpy.installed_solvers())"`; install `highspy` if HIGHS is unavailable.
- Browser tests require Playwright and Chromium: `python -m pip install playwright`, then `python -m playwright install chromium`. Tests skip if the dependency/browser is absent (they also try installed Chrome).
- Existing databases, CSV, Excel, pickle outputs, caches, and virtual environments are ignored by Git. Avoid treating these local artifacts as source.
- No checked-in CI workflow, formatter/type-checker configuration, or package build configuration was found.

## Test map

| Tests | What they cover |
| --- | --- |
| [test_custom_chart_sql.py](../../tests/test_custom_chart_sql.py) | SQL/pandas parity, batching, selected-field reads, grouping/splits, timezone/DST, minute telemetry, tariff quoting |
| [test_dashboard_forecasts.py](../../tests/test_dashboard_forecasts.py) | Raw/adjusted selection, summary semantics, battery-level custom charts, stable colours/dashes/markers |
| [test_grid_power_rollup.py](../../tests/test_grid_power_rollup.py) | R04 sign-separated import/export means, zeros/nulls, tariff-split hours, raw/hourly parity, incremental/rebuild statistics and interrupted legacy-to-current upgrade |
| [test_review_recovery.py](../../tests/test_review_recovery.py) | R01 finite/neutral residuals and real adjusted writes; R02 startup/midnight baseline retry and partial PV rollback; R03 transactional creation/rebuild, interruption/retry and no repeated history scan |
| [test_analysis_download.py](../../tests/test_analysis_download.py) | CSV/Excel downloads, SQL/pandas parity across every timestep and DST, optional/empty forecasts, single-query execution/forecast skipping, tariff labels, XlsxTurbo Excel values/dates/blanks |
| [test_solar_adjustment.py](../../tests/test_solar_adjustment.py) | Older tree model's rolling windows, as-of features, cumulative/day Wh |
| [test_forecast_schema_migration.py](../../tests/test_forecast_schema_migration.py) | Legacy forecast renaming and repeat-open preservation |
| [test_sigenstor_schema_migration.py](../../tests/test_sigenstor_schema_migration.py) | Battery-availability rename preserves values |
| [test_database_0_1_0_migration.py](../../tests/test_database_0_1_0_migration.py) | Current opener preserves legacy readings/primary keys, renames availability, publishes rollup readiness and tolerates repeat opens; historical filename retained |
| [test_config.py](../../tests/test_config.py), [test_common.py](../../tests/test_common.py), [test_control.py](../../tests/test_control.py) | Backup-rule parsing, exception helper, recalculation command, restart marker |
| [test_live_grid_energy_day_start.py](../../tests/test_live_grid_energy_day_start.py) | Configured-timezone meter baseline |
| [test_dashboard_freshness_browser.py](../../tests/test_dashboard_freshness_browser.py) | Offline/socket/server-silence/resume/stale behavior; isolated NiceGUI test server |

## Current follow-up verification: 2026-10-15

Full collection is restored (R18). [test_remaining_review_fixes.py](../../tests/test_remaining_review_fixes.py) adds independent numerical/recovery checks for R05-R17, including the real HiGHS solver across both DST transitions. See [follow-up verification](../reviews/system-review-follow-up.md) for current suite counts, browser evidence and model benchmarks.

## Original review verification: 2026-10-14

- Full `python -m pytest -q`: collection stopped because `migrate_database_0_1_0` is missing.
- Temporary subset: `python -m pytest -q --ignore=tests/test_database_0_1_0_migration.py` → **82 passed, 1 skipped** (Playwright module unavailable), 215 dependency/NumPy deprecation warnings.
- HiGHS was available in the reviewed environment. Pytest was installed into the existing `.venv` to run checks.
- No live device/API collection, production database migrations, backfills, or planning workbook execution was performed.
- Documentation links were checked against local files. These results describe the review snapshot, not future revisions.

## Appropriate verification

- Storage changes: use temporary SQLite fixtures; check physical/raw units, repeat opening, existing values, resets, and rollup consistency.
- Forecast changes: exercise the operational model and chronological replay, including cold start, missing panels, future-data exclusion, and DST boundaries. Existing tree-model tests do not establish production-model correctness.
- Planning changes: test energy conservation, efficiency losses, feasible/infeasible limits, scenario independence, and interval coverage. The root `test_energy_plan_as_of.py` is a report tool, not automated coverage.
- UI changes: check actual desktop/mobile rendering, live updates, preserved selections, reconnect/resume, missing data, and export failures. Unit tests alone do not establish visual correctness.
