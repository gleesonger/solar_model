# System review: findings and assessment

Reviewed **2026-10-15**, revision `3932b02f6de46bc74dc7392da0022726cf7af1d5`, including existing documentation changes and the untracked load-profile experiment. **The original review applied no fixes. Follow-up R01-R19 corrections are now implemented in the working tree; production data was not changed during development.** See the [plan](system-review-plan.md), [coverage matrix](system-review-coverage.md) and [recommended work](system-review-recommendations.md).

## R05-R19 follow-up: implemented 2026-10-15

All confirmed finding IDs now have fixes and targeted verification. See [follow-up details and measurements](system-review-follow-up.md). Unresolved policy questions and hardware/deployment/replay coverage limits remain separate. R17 uses explicitly labeled calendar-day navigation for hour ranges; the date-only anchor remains. Earlier sections below retain the original test counts and upgrade sequence.

## R04 follow-up: implemented 2026-10-15

- Hourly statistics now store `plant_grid_import_power_kw`, `plant_grid_export_power_kw` and `grid_power_sample_count`. Sign magnitudes are accumulated before summation; NULLs are excluded, zeros counted, and all-missing intervals remain missing.
- Active chart SQL and the retained hourly frame reader use these statistics. Observations +2/-2 kW now produce import/export means 1/1 kW rather than 0/0. Signed net power and meter-energy fields retain their separate meanings.
- Existing databases, including ones with the R03 v1 marker, add the fields and rebuild hourly history once under `BEGIN IMMEDIATE`, committing the new `hourly_rollup_initialized_v2` marker atomically. Interrupted upgrades roll back and retry; later opens do not rescan. Raw samples remain unchanged.
- [Regression tests](../../tests/storage/test_grid_power_rollup.py): 18 cases cover mixed/all-import/all-export/zero/null observations, tariff splits, incremental versus rebuilt statistics, and normal/interrupted legacy upgrades. Broader documented subset: **178 passed, 1 skipped** (known missing migration module excluded). R06 was subsequently addressed with v3 valid counts.

## R01-R03 follow-up: implemented 2026-10-15

- **R01:** residual inference excludes missing/non-finite observations and invalid distributions/weights; no usable observations gives neutral correction rather than NaN. An integration fixture verifies finite adjusted watts/Wh are stored and raw forecasts retained.
- **R02:** grid/PV baseline and day state publish only after successful processing. Startup/midnight failures retry; later PV failures leave integration unchanged for a clean retry.
- **R03:** hourly creation, full initialization/recovery and `database_metadata.hourly_rollup_initialized_v1 = "1"` (subsequently superseded by R04 v2) commit under explicit `BEGIN IMMEDIATE`. Older unmarked databases receive a one-time raw-history rebuild; later marked opens do not rescan. Rollback/retry is tested before and after insertion, including recreation with an old marker.
- Verification: [test_review_recovery.py](../../tests/regressions/test_review_recovery.py), **23 targeted tests passed**. Documented broader subset: **160 passed, 1 skipped**, 1770 warnings. The existing missing migration module still requires exclusion; the project environment still skips its browser module. No full-suite pass is claimed.
- First deployment may have a longer, write-locking initialization for old databases; see the [runbook](../operations/runbook.md). The remaining review findings are unchanged. Original independent assessments and forensic scripts retain their historical failures; use regression tests to validate fixes.

## Assessment

The principal concerns are incorrect telemetry aggregation and recovery after partial failures. Core energy/cost arithmetic and raw/hourly transaction atomicity passed controlled checks, but passing tests do not establish complete correctness: independent fixtures reproduced failures absent from the existing suite.

Three agents began without conversation history and submitted separate assessments: [data](system-review-data-initial.md), [website](system-review-browser-initial.md), and [architecture/consistency](system-review-architecture-initial.md). The coordinator reran the data harness and architecture recovery/recalculation probes, independently built date-boundary/panel-selection fixtures, and inspected browser evidence. Source inspection, agent-only observations and unresolved policies are distinguished below.

The [website supplement](system-review-browser-supplemental.md) records cross-check adjudication and additional tariff/custom-chart/Control/Reload workflows. Initial assessments were retained unchanged. In total, the confirmed-finding list contains **5 high-priority and 14 medium-priority items**, including reconfirmed existing issues, a performance finding and the test-collection blocker; it does not count policy questions as defects. R01-R19 have since been addressed; the reproductions below describe the original reviewed behavior, not current behavior.

**Coverage limits remain material.** Hardware/provider behavior, a target Docker build, real process kills during migration, sustained concurrent SQLite lock pressure, complete replay/live parity and every browser edge-case combination were not verified. Browser fixtures use fake live readings and mocked administrative actions; they do not establish real control/restart recovery. This report is not an assertion that all website outputs or all deployment scenarios are correct.

## Confirmed findings

Priorities reflect demonstrated impact, not estimated occurrence in the user's production history. All defects below have high confidence in the stated reproduction; supported but less common configurations are explicitly identified.

| ID | Priority | Finding and concrete evidence | Detailed evidence |
| --- | --- | --- | --- |
| R01 | High (fixed) | A missing completed actual hour introduces a NaN residual, causing later adjusted forecasts to fail persistence (`cannot convert float NaN to integer`). Raw forecasts survive. | [Data DATA-01](system-review-data-initial.md); coordinator reran harness |
| R02 | High (fixed) | Live grid/PV day markers advance before baseline loading succeeds. One transient failure prevents retry: PV then raises `KeyError(1)`; grid reports 0/0 instead of 10/5 kWh. | [Architecture A2](system-review-architecture-initial.md); independent [recovery results](system-review-evidence/recovery-results.json) |
| R03 | High (fixed) | Interrupted initial hourly backfill is not resumed. The next opener succeeds with one raw row and zero hourly rows because table existence is treated as completion. | [Architecture A1](system-review-architecture-initial.md); independent recovery results |
| R04 | High (fixed) | Hourly grid telemetry splits import/export after signed samples have cancelled. Samples +2/-2 kW produce import/export means 0/0 instead of 1/1 kW. Meter-energy totals are a different calculation and are not implicated by this reproduction. | [Data DATA-02](system-review-data-initial.md); coordinator reran active chart-query fixture |
| R05 | High (fixed) | Tariff recalculation commits raw changes before rebuilding hourly data. Injected rebuild failure leaves raw rate/cost 0.5 and hourly 0.1, while reporting "no changes committed". Existing finding reconfirmed. | [Recalculation evidence](system-review-architecture-supplemental.md); coordinator reran probe |
| R06 | Medium (fixed) | Hourly telemetry divides by all samples rather than each field's valid count. Solar 4/NULL kW averages to 2 rather than 4; minute and hourly paths diverge. | [Data DATA-03](system-review-data-initial.md); coordinator reran fixture |
| R07 | Medium (fixed) | Analysis table totals sum power and battery levels. The browser fixture shows available battery 38 kWh and SOC 230% from four readings of 8/9/10/11 kWh and 50/55/60/65%. These are not meaningful totals of state measurements. | [Website assessment](system-review-browser-initial.md); coordinator inspected rendered table evidence and `ChartDataDisplay`/chart declarations |
| R08 | Medium (fixed) | End-of-day rounding admits the next midnight in exports and telemetry-only chart queries. Selecting October 14 exports October 14 and 15, totaling 11 kWh instead of 1; the minute-source/day-grouped power query also returns both dates with 1/10 kW. | Independent [coordinator results](system-review-evidence/coordinator-results.json) |
| R09 | Medium (fixed) | Recent forecast collection dates still depend on the host timezone. A September 10 00:15 Asia/Kolkata collection is treated as September 9 on the reviewed host; the selected-day 2 kWh forecast becomes missing. Existing timezone finding narrowed to an active path. | Coordinator results; [daily summary loader](../../solar_model/storage/queries/dashboard.py) |
| R10 | Medium (fixed) | Latest/BoD adjusted selectors require panel-1 fields. Valid panel-2-only same-day forecasts are ignored in favor of an older collection. Existing configuration question now reproduced. | Coordinator results; `load_latest_adjusted_forecast_for_day` / `load_bod_adjusted_forecast_for_day` |
| R11 | Medium (fixed) | Distinct subhour forecast targets overwrite one another through an hour-keyed map. Identity calibration maps 1000/2000/3000 W at 10:00/10:30/11:00 to 2000/2000/3000 W. Actual provider cadence was not captured. | [Data DATA-04](system-review-data-initial.md); coordinator reran fixture |
| R12 | Medium (fixed) | Rebuilding a local day in a non-hour timezone uses partial UTC-hour bounds. Kathmandu fixture leaves the opening bucket stale and drops the closing bucket's neighboring-day sample: expected sums 3/3, counts 2/2; actual sums 2/2, counts 2/1. | [Data DATA-05](system-review-data-initial.md); coordinator reran fixture |
| R13 | Medium (fixed) | Planner overlap/duration arithmetic uses local wall time across DST. One real hour receives 1.333333 kWh in autumn or 0.8 kWh in spring instead of 1; the repeated-hour interval can be rejected as zero duration. | [Data DATA-06](system-review-data-initial.md); coordinator reran fixture |
| R14 | Medium (fixed) | Forecast evaluation includes the currently incomplete actual hour despite its completed-hour contract. One current-hour sample yields one evaluation pair before hour end. | [Data DATA-07](system-review-data-initial.md); coordinator reran clock-recorded fixture |
| R15 | Medium (fixed) | If the second child launch fails, the supervisor does not terminate/reap the already-started first child. Mocked `Popen` proves missing cleanup; an actual orphan OS process was not launched. | [Architecture A3](system-review-architecture-initial.md); coordinator reran mock |
| R16 | Medium (fixed) | Forecast correction loads lifetime inputs and repeatedly constructs old training frames before applying the scalar lookback cutoff. One-year synthetic inputs occupy 250.4 MiB; load 2.56s plus one target-day calculation 11.64s, excluding other target days/writes. This is scaling evidence, not a measured production cadence breach. | [Architecture A4](system-review-architecture-initial.md); timings reviewer-measured, source coordinator-checked |
| R17 | Medium (fixed) | Hour-based Analysis navigation operates on a `date`: with Last=6 hours, back moves a whole day and forward does not move. A date-only anchor cannot navigate six-hour windows as the controls imply. | Coordinator arithmetic probe and [Analysis controls](../../solar_model/dashboard/tabs/analysis.py); exact intended navigation remains a UI decision |
| R18 | Medium (fixed) | Full pytest collection fails because `migrate_database_0_1_0` is missing. The documented passing subset excludes this module. | Coordinator full run; existing finding reconfirmed |
| R19 | Medium (fixed) | Mobile custom-chart controls/plot collapse, and a persistent stale-data banner obstructs the bottom Close action. At 390×844 the Variables field is 48.63px and preview 78.74px wide; clicking bottom Close times out with banner pointer interception. Top close/Escape remain alternatives. | [Website B3](system-review-browser-initial.md); independent [mobile results](system-review-evidence/mobile-validation.json) and [screenshot](system-review-evidence/mobile-custom-validated.png) |

### Additional detail for coordinator fixtures

Run from the repository root with the project interpreter:

```powershell
.venv\Scripts\python.exe documentation/reviews/system-review-evidence/coordinator-checks.py
.venv\Scripts\python.exe documentation/reviews/system-review-evidence/recovery-checks.py
```

These create only temporary databases. [Recorded results](system-review-evidence/coordinator-results.json) contain the exact dates, expected values and observed values. The date probe also shows Last=25 hours moving back two dates but forward one date.

- **R08:** [export SQL](../../solar_model/storage/queries/exports.py) and [chart SQL](../../solar_model/storage/queries/analysis.py) round `time.max` to the following midnight and apply `<= end`. Telemetry-only charts omit the additional wall-time filter used by mixed energy queries, so the extra source date survives. Use exact bounds or a half-open following-midnight bound. Acceptance: a next-midnight sample must not appear in the preceding day's CSV/XLSX or telemetry output, across relevant timesteps/forecast flags. Preserve distinct DST hours, partial ranges and selected-day final-second samples; check mixed query routes separately.
- **R09:** replace host-local date selection in active summaries with configured-timezone selection. Acceptance: the same fixture produces 2 kWh regardless of host timezone; test both sides of configured midnight and lifetime-start selection. Do not change intentional forecast vintage policies in the same fix.
- **R10:** choose valid adjusted snapshots using the configured/available panels, with an explicit partial-panel policy. Acceptance: a panel-2-only installation selects its same-day latest/first adjusted vintage; panel-1 installations retain their existing behavior.
- **R17:** decide whether hour navigation needs a datetime anchor or should be explicitly day-based. Acceptance: each arrow behaves symmetrically and matches its label for 1/6/23/24/25-hour selections, on desktop/mobile and across DST. This review verifies the asymmetry; it does not select the product behavior.
- **R18:** restore the intended migration module or reconcile obsolete coverage with the current migration contract. Acceptance: the ordinary full test command collects successfully and still verifies the intended schema/version behavior.
- **R19:** make the custom-chart controls/legend responsive and keep dialog actions above or clear of persistent warnings. Acceptance: at 390×844 and 360×800 select Solar/Load, refresh, toggle legends and use bottom Close with the stale banner present; no collapsed controls/plot or blocked action. The second viewport remains a repair acceptance check, not a claimed review run.

For R01–R07 and R11–R16, the linked independent assessments give affected functions, corrections, scope/risks and acceptance criteria. The original review applied no fixes; follow-up status is recorded above.

## Policy questions and unproven concerns

- **Physical forecast bounds:** the production prediction helper can output -900 W from a positive 100 W raw forecast under an adverse residual. Reproduced by both reviewer and coordinator, but clipping/allocation policy is not decided. Choose whether physical generation must be nonnegative before changing the model; see DATA-08. Do not silently interpret negative model scores as physical generation.
- **Deployment trust boundary:** source has no dashboard authentication; reachable browsers receive administrative actions. Actual Internet exposure and intended LAN/proxy restrictions are unknown. This is a confirmed design property, not proof of an exploitable public deployment.
- **Forecast interval and vintage semantics:** planning versus dashboard interval conventions, live/replay future-day parity, fixed 24-clock-hour summaries and attribution of long meter gaps remain documented questions. This review does not claim exhaustive dynamic verification of them.
- **Battery export aggregation:** selecting the last `_kwh` reading is explicit in the implementation guide, while overview wording describes cumulative meters versus other numeric averages. Availability is a state level, making that overview ambiguous. The website reviewer recorded five 11-versus-9.5 discrepancies against an averaging expectation; the coordinator does not promote these to proven calculation defects. Clarify the documentation and field contract; change output only after an explicit semantic decision.
- **Dependencies:** direct metadata/install checks found no broken project requirements. Native wheel availability was checked, but no target Docker build or complete transitive license/security audit was performed. Current venv has HiGHS; the system interpreter initially did not. Do not report that interpreter difference as a project solver defect.

## What passed and what the tests establish

- Independent arithmetic confirmed fixed-point scaling, normal counter deltas, reset-to-missing behavior, PV trapezoidal integration and basic tariff costs.
- Raw/hourly writes rolled back together on injected failure. Duplicate timestamps were rejected without incrementing aggregates or advancing prior-reading state. Restart baseline reload passed.
- Cache refresh failure retained the last snapshot; recovery advanced its version; one subscriber failure did not stop other subscribers.
- A bounded two-period plan solved with actual HiGHS and had zero independently calculated solar/load/battery/cost balance residual. This does not prove all planning constraints/scenarios.
- Browser energy/cost checks, interaction coverage and gaps are recorded in the [coverage matrix](system-review-coverage.md), not inferred from server tests.
- Project full `pytest -q` failed collection as R18. Project subset: **137 passed, 1 skipped**, 1769 warnings (coordinator run 24.75s). The skipped Playwright module was unavailable in that interpreter.
- Separately, `python -m pytest -q tests/test_dashboard_freshness_browser.py --disable-warnings` passed **5 tests** in 10.78s using the system Python/browser environment. This is a separate browser test run, not a full project-suite pass.

Environment and commands are retained in [baseline.json](system-review-evidence/baseline.json). Existing download speed measurements were reused rather than rerunning a benchmark without a new question. New scale measurements target the forecast preparation path.

## Consolidation assessment

Consolidation is justified where it prevents demonstrated divergence:

1. Define raw/hourly telemetry statistics together, including per-field counts and sign-separated grid sums, before sharing readers or rebuilding history.
2. Share UTC duration/overlap and explicit configured-timezone boundary mechanics, while preserving different chart/export/vintage contracts.
3. Consolidate equivalent tariff parsing/matching predicates between validation and runtime; keep validation and effective-date selection policies separate.
4. Isolate retained ORM/pandas reference paths from active SQL adapters once their callers and compatibility purpose are mapped. Keep independent expected-value tests; parity between two implementations can preserve the same defect, as R08 demonstrates.
5. Bound latest-device-history queries to one row per variable instead of materializing lifetime changes in two callers.

Two XLSX writers have justified separate uses: XlsxTurbo for fast flat dashboard downloads, XlsxWriter for report workbooks. No evidence supports replacing both merely to reduce library count. See architecture assessment for migration risk and verification for each recommendation.
