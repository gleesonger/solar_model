# Review findings

The **2026-10-15 deep review** is recorded in the [system review report](system-review-report.md), with a [coverage matrix](system-review-coverage.md) and [recommended work](system-review-recommendations.md). It confirms several findings below and adds independently reproduced data, recovery and browser issues. Its explicit verification limits apply; no implementation fixes were made by that review.

Original documentation review: 2026-10-14. The lists below reflect remaining items after the 2026-10-15 application fixes; historical review reports retain their original observations.

## Remaining defects and inconsistencies

| Finding | Evidence / consequence |
| --- | --- |
| Host timezone leaks into ORM local dates | [database.py](../../database.py) hybrids use host `astimezone()`/SQLite `localtime`; legacy hybrids still expose host dates; active recent/lifetime summaries now use configured timezone (R09). Configured timezone can differ from host, especially Docker. |
| Missing planning rules pass config load | Minimum/maximum schedules default empty and validate only when non-empty, but [simulate_energy_plan](../../energy_plan_simulation.py) calls `tariff_rule` on both unconditionally. |
| Solar interval conventions disagree | Dashboard treats forecast timestamps as starts; [load_forecast_as_of](../../test_energy_plan_as_of.py) uses them as ends and uses collection interval for the first interval width. Planning may shift energy timing. |
| Metadata has no global schema version | `database_metadata` tracks hourly readiness, not a global semantic database version. Migration regressions test the actual opener contract. |
| Docker port metadata differs | [Dockerfile](../../Dockerfile) exposes 8080; shipped YAML serves 8090. Explicit correct port mapping still works. |
| Tariff extraction still uses the old availability column | [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) queries `inverter_battery_available_discharge_kwh`; current schema renames it to `plant_battery_available_discharge_kwh`. The manual extractor needs reconciliation before running against a migrated database. |
| Historical metric additions remain incomplete | Unrelated new hourly metrics still require an explicit backfill policy; v3 rebuilds the reviewed telemetry statistics, but derived minute PV columns are also not backfilled by the opener. Schema presence does not imply historical populated values. |

## Model and data questions requiring decisions

- **Negative adjusted solar:** [operational_model._prediction_fields](../../forecast/operational_model.py) does not clamp additive residual output. Negative W/Wh is possible by inspection; decide physical bounds and test adverse residuals.
- **Replay parity:** live correction handles future target days in each collection; historical rebuild clears all selected targets but replays only each collection's current local day. Confirm the intended audit coverage before relying on rebuilt future-day vintages.
- **DST representation:** several views use fixed 24 clock-hour buckets, merging repeated hours. Spring DST SQL tests exist; UTC planning arithmetic and both DST transitions now have regression coverage (R13); fixed-clock dashboard views and broader local display still need separate verification.
- **Interval attribution:** counter gaps use the ending timestamp's tariff/hour and PV integration spans the whole gap. Decide acceptable maximum gap and whether tariff/day boundaries require interval splitting.
- **Forecast export meaning:** export aggregation averages cumulative `*_watt_hours` while charts difference them into interval energy. Confirm whether downloads should expose stored provider fields or reproduce chart energy.
- **Scenario interpretation:** optimiser decisions are independent across scenarios. If the goal becomes one executable plan under uncertainty, shared-decision constraints and a control integration would be required.
- **Plan model implementation decisions:** the [proposed plan model](../overview/plan-model.md#63-assumptions-and-unresolved-decisions) specifies a shared next action and normalised cost/protection scoring. Open items include verified EMS command response and handover, consistent usable-energy/SOC conventions, future-date load coverage, input fallback, and approximation/solver tolerances. This is a target design; current planning remains report-only.
- **Validation policy:** unknown YAML keys are ignored and some numeric/site fields lack bounds. Decide whether descriptive keys should be formal fields and other unknown keys rejected.

## Coverage and maintenance gaps

- Chrome Android saved offline pages can preserve dashboard visuals while in-page scripts are inactive. Finite CSS activity indication and an absolute Last reading timestamp reduce ambiguity, but actual saved-page/freeze behaviour still needs verification on Android hardware; desktop Chromium regression checks do not establish it.

- Runtime dependencies are ranged, with no lockfile; test/browser/extraction dependencies and explicit HiGHS installation are not declared together.
- Tariff helper logic is duplicated between config validation and economics; changes must preserve matching parity.
- `dashboard/data.py` retains old aggregation paths beside the active SQL adapters; `HourlyTab` is unmounted. Clarify their retained compatibility purpose before removal.
- Independent `check-tariffs` scripts run on import and extraction has a hard-coded external database path; treat them as manual tools.

## Suggested repair order

1. Resolve forecast interval meaning and required planning-rule validation.
2. Extend replay parity and adverse optimiser coverage before changing those contracts.
3. Reconcile dependency declaration, port metadata, and retained legacy paths.

## Application fixes completed

- **2026-10-19, browser freshness:** short tab switches keep a fresh heartbeat. The three-dot live-update indicator expires through finite CSS with wall-clock/lifecycle checks, clears on unhealthy status, and has a reduced-motion variant. A small light-grey collection timestamp remains below Live / Today, including in static copies.

- **2026-10-15, Recent summary performance:** daily/monthly/lifetime tables now share one native SQL aggregation, replacing overlapping ORM/pandas history processing. Full twelve-month presentation and previous numeric results are preserved; see [measurements and validation](recent-summary-performance.md).

- **2026-10-15, R05-R19:** tariff raw/rollup atomicity, per-field valid counts with v3 upgrade, table totals, exact date bounds, configured-timezone summaries, configured-panel selectors, exact forecast targets, non-hour-zone rebuilds, UTC planning, completed-hour evaluation, partial-launch cleanup, bounded model preparation, symmetric day-based hour navigation, full pytest collection and mobile custom dialogs are addressed. See [follow-up verification](system-review-follow-up.md). Remaining policies and coverage limits above are unchanged.

- **2026-10-15, R04:** hourly import/export power now accumulates signs separately and counts valid grid observations; chart SQL and retained hourly readers use the corrected means. V2 initialization atomically rebuilds historical rollups once. Added 18 regression cases; documented broader subset 178 passed/1 skipped. Other nullable telemetry counts were subsequently addressed by R06/v3.

- **2026-10-15, R01-R03:** missing/non-finite observed hours are excluded from residual inference; live daily baseline failures retry without publishing partial state; hourly initialization/recovery is transactional and completion-marked. Legacy unmarked databases rebuild once on next open; raw history is retained. Regression checks: 23 new cases, broader documented subset 160 passed/1 skipped. See the [review report](system-review-report.md) and [runbook](../operations/runbook.md).

- **2026-10-15:** Analysis downloads now aggregate text tariff bands as distinct labels instead of attempting a numeric average. The dialog imports the shared error notifier and logs the underlying failure before restoring the form. Regression checks cover CSV/Excel output, optional forecasts, numeric aggregation, mixed/repeated bands, and missing labels in temporary databases.

## Documentation corrections completed

- **2026-10-15:** added a worked metric-extension recipe, temporary synthetic-dashboard walkthrough, backup/restore recovery procedures and a cross-output metric semantics reference. Corrected operational-model input loading and lifetime timezone descriptions. Availability downloads are now consistently documented as last-reading output; changing that behavior remains a policy decision.

- Root README now links these guides and corrects unknown-key behavior, effective-dated tariff YAML, table/storage descriptions, and live/shared/Analysis refresh behavior.
- Existing warning and notification descriptions were preserved. Remaining model/runtime limitations are described in the subsystem guides and findings above.
