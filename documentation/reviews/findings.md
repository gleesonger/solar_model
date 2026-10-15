# Review findings

Reviewed 2026-10-14 against the working tree described in the [documentation index](../index.md). Findings below were documented; application fixes are outside this documentation change.

## Confirmed defects and inconsistencies

| Finding | Evidence / consequence |
| --- | --- |
| Full test suite cannot collect | [test_database_0_1_0_migration.py](../../tests/test_database_0_1_0_migration.py) imports absent `migrate_database_0_1_0`. Full pytest run reproduced `ModuleNotFoundError`. |
| Download error handler lacks import | [download_data_dialog.py](../../dashboard/download_data_dialog.py) calls `notify_error` without importing it. A validation/export failure reaches an undefined name. |
| Host timezone leaks into ORM local dates | [database.py](../../database.py) hybrids use host `astimezone()`/SQLite `localtime`; some summary/export queries use those hybrids. Configured timezone can differ from host, especially Docker. |
| Recalculation error message overstates rollback | [recalculate_economics.py](../../recalculate_economics.py) commits raw updates before rebuilding rollups, yet caught failures say “no changes committed”. |
| Missing planning rules pass config load | Minimum/maximum schedules default empty and validate only when non-empty, but [simulate_energy_plan](../../energy_plan_simulation.py) calls `tariff_rule` on both unconditionally. |
| Solar interval conventions disagree | Dashboard treats forecast timestamps as starts; [load_forecast_as_of](../../test_energy_plan_as_of.py) uses them as ends and uses collection interval for the first interval width. Planning may shift energy timing. |
| Metadata table is not a version mechanism yet | `database_metadata` exists, but current `open_database()` writes no version. The missing standalone migration test expects `0.1.0`. |
| Docker port metadata differs | [Dockerfile](../../Dockerfile) exposes 8080; shipped YAML serves 8090. Explicit correct port mapping still works. |
| Tariff extraction still uses the old availability column | [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) queries `inverter_battery_available_discharge_kwh`; current schema renames it to `plant_battery_available_discharge_kwh`. The manual extractor needs reconciliation before running against a migrated database. |
| Historical metric additions remain incomplete | Existing hourly tables get new columns without rebuilding old rows; derived minute PV columns are also not backfilled by the opener. Schema presence does not imply historical populated values. |

## Model and data questions requiring decisions

- **Negative adjusted solar:** [operational_model._prediction_fields](../../forecast/operational_model.py) does not clamp additive residual output. Negative W/Wh is possible by inspection; decide physical bounds and test adverse residuals.
- **Replay parity:** live correction handles future target days in each collection; historical rebuild clears all selected targets but replays only each collection's current local day. Confirm the intended audit coverage before relying on rebuilt future-day vintages.
- **Panel-1 assumption:** newest/BoD dashboard adjusted selectors require panel-1 fields, though configuration permits installations using only panels 2–4. Also consider partially collected snapshots and the cold-start fallback difference between model and dashboard.
- **DST representation:** several views use fixed 24 clock-hour buckets, merging repeated hours. Spring DST SQL tests exist; end-to-end autumn DST planning, totals, and local display still need verification.
- **Interval attribution:** counter gaps use the ending timestamp's tariff/hour and PV integration spans the whole gap. Decide acceptable maximum gap and whether tariff/day boundaries require interval splitting.
- **Forecast export meaning:** export aggregation averages cumulative `*_watt_hours` while charts difference them into interval energy. Confirm whether downloads should expose stored provider fields or reproduce chart energy.
- **Scenario interpretation:** optimiser decisions are independent across scenarios. If the goal becomes one executable plan under uncertainty, shared-decision constraints and a control integration would be required.
- **Validation policy:** unknown YAML keys are ignored and some numeric/site fields lack bounds. Decide whether descriptive keys should be formal fields and other unknown keys rejected.

## Coverage and maintenance gaps

- Temporary test subset passed **82 tests**; one Playwright module skipped. This does not establish a healthy full suite or actual mobile/browser rendering.
- Current forecast-adjustment tests exercise the older tree model, not the operational scalar/residual implementation.
- No automated optimiser conservation/feasibility tests were found; the root `test_...` file is a CLI.
- Runtime dependencies are ranged, with no lockfile; test/browser/extraction dependencies and explicit HiGHS installation are not declared together.
- Tariff helper logic is duplicated between config validation and economics; changes must preserve matching parity.
- `dashboard/data.py` retains old aggregation paths beside the active SQL adapters; `HourlyTab` is unmounted. Clarify their retained compatibility purpose before removal.
- Independent `check-tariffs` scripts run on import and extraction has a hard-coded external database path; treat them as manual tools.

## Suggested repair order

1. Restore/reconcile the missing migration test dependency and import the download error notifier.
2. Unify configured-timezone handling, forecast interval meaning, and required planning-rule validation.
3. Add production-model/replay and optimiser checks before changing their algorithms.
4. Reconcile dependency declaration, port metadata, and retained legacy paths.

## Documentation corrections completed

- Root README now links these guides and corrects unknown-key behavior, effective-dated tariff YAML, table/storage descriptions, and live/shared/Analysis refresh behavior.
- Existing warning and notification descriptions were preserved. Remaining model/runtime limitations are described in the subsystem guides and findings above.
