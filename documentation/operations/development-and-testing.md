# Development and testing

## Setup

Run from the repository root. Docker uses Python 3.12.10; the reviewed local virtual environment uses Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

- [requirements.txt](../../requirements.txt) contains runtime ranges rather than a lockfile. Pytest, Playwright and extraction's openpyxl are provided by package extras rather than the runtime list.
- Planning explicitly requests HiGHS; `highspy` is a declared runtime dependency. Verify with `python -c "import cvxpy; print(cvxpy.installed_solvers())"`.
- Browser tests require Playwright and Chromium: `python -m pip install playwright`, then `python -m playwright install chromium`. Tests skip if the dependency/browser is absent (they also try installed Chrome).
- Existing databases, CSV, Excel, pickle outputs, caches, and virtual environments are ignored by Git. Avoid treating these local artifacts as source.
- `pyproject.toml` defines the installable package, console scripts, package data and optional `test`, `browser`, `research` dependencies. No CI or formatter/type-checker configuration is configured.

## Package verification

Tests are grouped by subsystem. Run `python -m pytest -q` from the repository root. `python -m pip wheel . --no-deps` verifies package construction; the wheel must include dashboard CSS/JS/SVG and both register maps. Module CLIs support `--help` without opening a database. Tests use temporary databases and synthetic browser data.

Verified on **2026-10-24** after the package restructure: **309 passed**, including the desktop/mobile browser checks and the in-memory planning API using HiGHS. The wheel built successfully; all five tool CLIs loaded from an extracted wheel in a separate configuration directory. Dashboard bootstrap found the packaged favicon and register maps and created only a temporary database. Documentation links were checked. No production database or hardware was used for this verification; a Docker image build was not performed.

Cleanup removed the superseded lab sketch, unmounted Hourly UI and unused helpers, old package folders, Python/pytest caches, generated build metadata and task-generated temporary scripts/wheels. Existing databases, research CSV/workbooks and archived review evidence were retained.

## Test map

Use the [isolated development walkthrough](isolated-development.md) for synthetic full-dashboard checks without hardware/provider calls. For a feature change spanning layers, follow the [worked metric recipe](../implementation/adding-a-metric.md).

| Tests | What they cover |
| --- | --- |
| [test_forecast_model_config.py](../../tests/forecasting/test_forecast_model_config.py) | YAML model parsing/defaults/validation, lookback and half-life effects, and model settings passed through collection, refresh and CLI entry points |
| [test_forecast_influence.py](../../tests/forecasting/test_forecast_influence.py) | Decimal-fraction load/intraday influence, sparse day-type fallback, 100% influence and invalid fractions and old percentage inputs |
| [test_daily_load_forecast.py](../../tests/forecasting/test_daily_load_forecast.py) | House-load profile freeze/restart, midnight cutoff, DST targets, scaling, missing history, planning parity, migration and Analysis-only forecast styling |
| [test_daily_load_forecast_browser.py](../../tests/browser/test_daily_load_forecast_browser.py) | Desktop/mobile forecast absence from Recent, hourly Analysis legend defaults and grouping changes with synthetic data |
| [test_forecast_schedule.py](../../tests/forecasting/test_forecast_schedule.py) | Persistent external-request cadence, restart/history fallback, exact due boundary, failed attempts, duplicate claims, UTC DST duration and schedule-database failure recovery |
| [test_custom_chart_sql.py](../../tests/dashboard/test_custom_chart_sql.py) | SQL/pandas parity, batching, selected-field reads, grouping/splits, timezone/DST, minute telemetry, tariff quoting |
| [test_recent_summary_sql.py](../../tests/dashboard/test_recent_summary_sql.py) | Native summary/Python parity, DST/timezone boundaries, nulls/resets/vintages, empty months, leap-day/lifetime totals and shared single-aggregation refresh |
| [test_dashboard_forecasts.py](../../tests/dashboard/test_dashboard_forecasts.py) | Raw/adjusted selection, summary semantics, battery-level custom charts, stable colours/dashes/markers |
| [test_grid_power_rollup.py](../../tests/storage/test_grid_power_rollup.py) | R04 sign-separated import/export means, zeros/nulls, tariff-split hours, raw/hourly parity, incremental/rebuild statistics and interrupted legacy-to-current upgrade |
| [test_review_recovery.py](../../tests/regressions/test_review_recovery.py) | R01 finite/neutral residuals and real adjusted writes; R02 startup/midnight baseline retry and partial PV rollback; R03 transactional creation/rebuild, interruption/retry and no repeated history scan |
| [test_analysis_download.py](../../tests/dashboard/test_analysis_download.py) | CSV/Excel downloads, SQL/pandas parity across every timestep and DST, optional/empty forecasts, single-query execution/forecast skipping, tariff labels, XlsxTurbo Excel values/dates/blanks |
| [test_forecast_schema_migration.py](../../tests/storage/test_forecast_schema_migration.py) | Legacy forecast renaming and repeat-open preservation |
| [test_sigenstor_schema_migration.py](../../tests/storage/test_sigenstor_schema_migration.py) | Battery-availability rename preserves values |
| [test_database_0_1_0_migration.py](../../tests/storage/test_database_0_1_0_migration.py) | Current opener preserves legacy readings/primary keys, renames availability, publishes rollup readiness and tolerates repeat opens; historical filename retained |
| [test_config.py](../../tests/core/test_config.py), [test_common.py](../../tests/core/test_common.py), [test_control.py](../../tests/dashboard/test_control.py) | Backup-rule parsing, exception helper, recalculation command, restart marker |
| [test_live_grid_energy_day_start.py](../../tests/dashboard/test_live_grid_energy_day_start.py) | Configured-timezone meter baseline |
| [test_dashboard_freshness_browser.py](../../tests/browser/test_dashboard_freshness_browser.py) | Offline/socket/server-silence/resume/stale behavior; CSS expiry without JS timeouts, reduced motion, indicator health gating, timestamp placement/style on phone/desktop widths; isolated NiceGUI test server |

## Browser freshness follow-up: 2026-10-19

`.venv/Scripts/python.exe -m pytest -q --disable-warnings` completed with **264 passed** (2,130 dependency/deprecation warnings). Playwright and Chromium were installed separately for local verification. Browser regressions exercised the real assets and a synthetic NiceGUI Recent table at 390px/1280px widths, including timestamp refresh/placement, light-grey 12px styling, CSS animation expiry without a JavaScript timeout, reduced motion, brief/long resume, offline/reconnect and stale-data suppression. No production database/hardware was used. Chrome Android saved offline archives still require actual-device verification.

## Current follow-up verification: 2026-10-15

Full collection is restored (R18). [test_remaining_review_fixes.py](../../tests/regressions/test_remaining_review_fixes.py) adds independent numerical/recovery checks for R05-R17, including the real HiGHS solver across both DST transitions. See [follow-up verification](../reviews/system-review-follow-up.md) for current suite counts, browser evidence and model benchmarks.

## Original review verification: 2026-10-14

- Full `python -m pytest -q`: collection stopped because `migrate_database_0_1_0` is missing.
- Temporary subset: `python -m pytest -q --ignore=tests/test_database_0_1_0_migration.py` → **82 passed, 1 skipped** (Playwright module unavailable), 215 dependency/NumPy deprecation warnings.
- HiGHS was available in the reviewed environment. Pytest was installed into the existing `.venv` to run checks.
- No live device/API collection, production database migrations, backfills, or planning workbook execution was performed.
- Documentation links were checked against local files. These results describe the review snapshot, not future revisions.

## Appropriate verification

- Storage changes: use temporary SQLite fixtures; check physical/raw units, repeat opening, existing values, resets, and rollup consistency.
- Forecast changes: exercise the operational model and chronological replay, including cold start, missing panels, future-data exclusion, and DST boundaries.
- Planning changes: test energy conservation, efficiency losses, feasible/infeasible limits, scenario independence, and interval coverage. `solar_model.tools.plan_as_of` is a report tool; planning API regressions live in `tests/planning/`.
- UI changes: check actual desktop/mobile rendering, live updates, preserved selections, reconnect/resume, missing data, and export failures. Unit tests alone do not establish visual correctness.
