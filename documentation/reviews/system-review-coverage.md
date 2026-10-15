# System review: coverage and limits

Reviewed **2026-10-15**. Read alongside the [report](system-review-report.md) and [independent website assessment](system-review-browser-initial.md). Status applies only to the listed scenario; a passing row does not certify every input combination.

**Important limits:** no real hardware/provider capture, production writes, target Docker build, actual migration process kill, sustained multi-process lock test or complete live/replay comparison. Browser checks use isolated services and synthetic records, not the production website. Numerical verification is representative and does not cover every plotted point/selection combination.

## R04 follow-up

R04 is fixed in the working tree: 18 new regression cases independently verify separate import/export magnitudes, valid grid counts, raw/hourly equivalence and historical upgrade/retry. Current documented project subset: **178 passed, 1 skipped**. The matrix records original review observations; R06 remains open for non-grid telemetry counts. No additional browser/hardware verification is implied.

## R01-R03 follow-up

The matrix below preserves original review coverage. R01-R03 have since been fixed: 23 regression cases cover residual validity/persistence, live startup/midnight retries and partial-state rollback, and interrupted hourly initialization/recreation. The project subset now returns **160 passed, 1 skipped** with the same missing migration module excluded. These server regressions do not close previously untested browser/hardware/deployment scenarios. See [current status](system-review-report.md).

## Environments and evidence

- Application baseline: project Python 3.12.10, SQLite 3.49.1, pandas 2.3.3, SQLAlchemy 2.0.52, NiceGUI 2.24.2, XlsxTurbo 1.11.0. See [baseline](system-review-evidence/baseline.json).
- Main browser fixture: isolated NiceGUI instance on `127.0.0.1:18091`, four electrical records, two forecast vintages, fake live readings and mocked Control actions. Separate empty-data instance used by the website reviewer. Browser environment uses the system interpreter/Playwright; do not equate it with the venv's skipped browser module.
- Browsers: separate contexts for state isolation; desktop and 390×844 mobile layouts. Fixture dates follow the execution day, so elapsed-forecast outputs are time-dependent unless a check explicitly freezes time.
- Source/test-only verification is marked separately from browser verification. Existing regression parity tests are supporting evidence, not independent numerical oracles.

Statuses: **Pass**, **Fail**, **Partial**, **Blocked**, **Not checked**, **Not applicable**. Partial includes representative coverage with stated gaps; Fail names a reproduced defect. A defect can coexist with other passing checks in the same feature family.

## Website feature matrix

| Feature/output and scenario | Interaction works | Values verified | Edge cases checked | Evidence / remaining limits |
| --- | --- | --- | --- | --- |
| Mounted Recent/Analysis/System Info/Control navigation | Pass | Not applicable | Partial | Actual isolated pages opened; unmounted Hourly component excluded from website inventory |
| Recent live power, daily meters, battery availability/SOC | Pass | Pass, fixture | Partial | Known fake values rendered; real Modbus ingestion not part of this browser check; R02 independently fails recovery |
| Recent per-array live allocation | Pass | Pass, fixture | Partial | Fake East/West values 1.8/1.2 kW and 3.6/2.4 kWh; underlying integration separately inspected, not exhaustively verified |
| Recent latest/BoD/raw forecast summaries | Pass | Partial | Partial | Display/vintage fixture exercised; elapsed-time totals not fully frozen; R09/R10 fail supported timezone/panel cases server-side |
| Past-seven-day energy/economics rows and Total | Pass | Pass, known populated row/totals | Partial | Fixture solar 4, load 2, import 1, export .4 kWh; import cost .40, revenue .08, net .32; no exhaustive calendar/gap matrix |
| Past-twelve-month and lifetime summaries | Pass | Partial | Partial | Rendered fixture totals and ordering inspected; lifetime/host-date and large-history behaviors are not fully certified |
| Analysis energy chart and table | Pass | Pass, known fixture points/totals | Partial | 08:00/09:00 solar 2 kWh each, load 1 each; browser series and exported values compared |
| Analysis power chart and table | Pass | Partial / Fail | Partial | Known minute points verified; Total sums kW (R07); multi-day rollup sign/null failures R04/R06 independently reproduced |
| Analysis battery chart and table | Pass | Partial / Fail | Partial | Points 8/9/10/11 kWh and 50/55/60/65%; misleading Total 38/230% (R07) |
| Analysis per-array energy chart and table | Pass | Partial | Partial | Rendered and CSV exercised; no every-panel/missing-panel browser matrix |
| Analysis money chart and table | Pass | Pass, selected fixture values | Partial | Cost/revenue/net/rate values checked; one-decimal chart rounding differs intentionally from cent-precision tables |
| Split-tariff checkbox | Pass, supplemental | Pass, fixture | Partial | Day/night series switch to one Import Cost totaling .40; no exhaustive legend-selection combinations |
| Chart/table toggles on all five charts | Pass | Partial | Partial | All toggles exercised; verified data subsets differ by chart as above |
| Chart-specific CSV on all five charts | Pass | Partial | Partial | All five downloaded; known energy/cost/telemetry values checked, not every field/row combination |
| Chart clipboard | Pass, energy chart | Pass, sampled values | Not checked for all five | Browser clipboard read exercised; shared implementation does not prove every button separately |
| Chart legends, hover tooltips and zoom | Partial | Partial | Not checked exhaustively | One legend selection survives Apply and chart API zoom dispatch reaches 30–50%; canvas hover screenshot taken. Pointer dragging and exact tooltip field comparisons remain unverified |
| As-of/Last/Period/Group-by/Aggregation controls | Pass, representative | Partial | Partial | All group-by choices applied; Sum/Average exercised; not every period/group/aggregation cross-product independently verified |
| Invalid Last input | Pass | Pass | Partial | Zero rejected with visible notification; malformed/non-finite combinations not exhaustively exercised |
| Hour-based date arrows | Fail | Fail | Partial | Six-hour back jumps one day; forward then remains unchanged (R17), observed in browser and independent arithmetic |
| Custom-chart empty/selected-variable refresh | Pass, representative | Pass, selected fixture | Partial | Empty and Solar previews; supplemental Solar+Load split Day sums 4/2 kWh. Full variable/split/style matrix remains unverified |
| Custom metric/split legend checkboxes | Pass, supplemental | Pass, selected fixture | Partial | Solar off leaves Load2; Thu split off removes all series; other splits/styles not exhaustively checked |
| Full-data CSV, every timestep, forecasts enabled | Pass | Pass, selected fields | Partial | Seven timesteps downloaded; fixture energy/cost totals, labels, mean power, counter last and stored forecast values independently checked |
| Full-data XLSX, hourly, forecasts disabled | Pass | Pass, selected fields | Partial | Workbook opened; two actual rows, solar 4 kWh, no forecast columns and local-hour value checked |
| Full-data format × timestep × forecast combinations | Partial | Partial | Partial | Browser runs are not a full cross-product; server regression checks supplement but do not replace it |
| Invalid full-data date range and retry form | Pass | Pass | Partial | Error surfaced and form restored; not every validation/failure path injected |
| Selected-day midnight boundary in exports/telemetry-only charts | Fail, server reproduction | Fail | Pass, targeted | R08: 1 selected-day kWh becomes 11; minute-source/day-grouped telemetry returns both dates with 1/10 kW. Not discovered by old/new parity tests |
| System Info metadata | Pass | Pass, fixture | Partial | Fixture model displayed; multi-version/invalid metadata not exhaustively browser-tested |
| Control restart cancellation/confirmation | Pass, mocked | Pass, action trace | Partial | Cancellation invokes nothing; confirmation records restart. Real process termination/reconnect not verified |
| Control recalculation success/failure | Pass, mocked | Pass, UI status/action trace | Partial | Success/failure messages exercised; actual isolated raw/rollup failure tested separately as R05 |
| Control date validation / recalculation cancel | Pass, supplemental | Pass, action log/status | Partial | Cancel and empty/reversed/future dates invoke no action and show appropriate errors; other branches remain untested |
| Offline/reconnect warning | Pass | Pass, observed messages | Partial | Actual browser offline transition/recovery tested; persisted stale data appropriately remains stale |
| Missing/stale live, heartbeat and socket state | Pass, controlled browser state | Pass, messages | Partial | Controlled browser metadata/socket probes plus five standalone warning tests; not real server death/restart |
| Healthy warning recovery | Pass | Pass | Partial | Fresh metadata/heartbeat clear warning; priority cases not all combined in full application |
| Warning Reload action | Pass, supplemental | Pass, resulting page | Partial | Isolated reload returns Recent/Live Today; no production restart involved |
| Two-browser filter isolation | Pass | Pass, grouping selection | Partial | Changing one session to day leaves other at hour; not all custom/download/control state combinations tested |
| Desktop rendering | Pass, sampled | Partial | Partial | Screenshots and actual tables/charts inspected; no exhaustive screen-size or browser-engine matrix |
| Mobile navigation, Analysis drawer/apply | Partial | Partial | Partial | Drawer opens/applies/closes, but Recent layout scroll width457px exceeds390px viewport; no separate cosmetic finding assigned |
| Mobile custom chart dialog | Fail | Partial | Fail, targeted | R19 independently reproduced: Variables48.63px, chart78.74px; stale banner intercepts bottom Close |
| Empty-data Recent/Analysis | Pass, bounded | Partial | Partial | Empty fixture renders five charts without browser errors; live remains fake and metadata-empty behavior not separately asserted |
| Console/network failures | Partial | Not applicable | Partial | Console captured for exercised paths; no deliberate CDN/network-dependency outage matrix |

The independent website report is authoritative for its precise action list and raw artifact locations. A sampled Pass above must not be expanded into a claim that all browser outputs passed.

Additional checks after initial submission are recorded in the [website supplement](system-review-browser-supplemental.md). Its final isolated servers shut down through test-only endpoints with exit 0; [cleanup evidence](system-review-evidence/browser/cleanup.json) records zero remaining processes/listeners. This verifies test cleanup, not production supervisor restart behavior.

Its numerical script made 126 comparisons: 121 passed and five expected a mean for availability where implementation documentation explicitly specifies last. Those five are retained as documentation/contract discrepancies, not five independently confirmed calculation failures. Coordinator adjudication is recorded in the main report; the initial assessment remains unchanged.

## Server, data and operational coverage

| Area/scenario | Status | Evidence / limits |
| --- | --- | --- |
| Signed register decoding / short response | Pass, bounded | Independent signed/unsigned arithmetic and short-response rejection; no hardware/sentinel/protocol-version validation |
| Scaling and normal persistence | Pass, bounded | 2.5 kW stored as 25000; energy deltas, costs and PV trapezoid independently checked |
| First sample, counter reset, duplicate and restart baseline | Pass, bounded | NULL first/reset delta; duplicate rollback; latest persisted baseline restored |
| Raw/hourly transaction rollback | Pass, injected failure | Both tables unchanged and previous state unchanged after pre-commit error |
| Forecast HTTP errors, partial panels, retries | Partial, source | Error isolation traced; no complete deterministic provider-response matrix or live API calls |
| Forecast NaN residual/write failure | Fail | R01; raw records survive failed adjusted write |
| Exact forecast target alignment | Fail | R11; subhour identity curve changes |
| Forecast panel selection | Fail | R10; panel-2-only latest/BoD selection falls back to older vintage |
| Configured timezone summary collection date | Fail | R09; 2 kWh becomes missing in Asia/Kolkata fixture |
| Calendar/DST grouping across all timesteps | Partial | Existing seven-timestep parity fixtures cover Dublin spring/autumn and Kolkata; independent boundary leak proves parity is insufficient |
| Non-hour timezone rollup rebuild | Fail | R12; opening/closing UTC buckets inconsistent |
| First hourly migration/backfill failure and reopening | Fail | R03; initialization not resumed after table creation |
| Schema rename and repeated opening | Partial | Existing migration tests passed in subset; actual interrupted DDL/concurrent migration not tested |
| Economics update plus rollup failure | Fail | R05; real temporary SQL commit followed by injected rebuild failure |
| Shared snapshot load failure/recovery/listener exception | Pass, isolated async mocks | Prior version retained, next refresh advances, another listener still notified |
| Live daily initialization failure/retry | Fail | R02; first-load and same-day retry probes; actual DB-lock timing not exercised |
| Supervisor partial launch cleanup | Fail, mocks | R15; first child not terminated/reaped if second launch raises; no real orphan spawned |
| Multi-process SQLite contention / collector-recalc overlap | Not checked dynamically | Transaction source traced; need controlled concurrent processes |
| Production correction scale | Partial | Synthetic 7/90/365-day timings; R16; no production latency/peak-RSS guarantee |
| Forecast completed-hour evaluation | Fail | R14; current-hour sample eligible immediately |
| Negative adjusted generation | Reproduced; policy open | -900 W model output; physical-bound policy not selected |
| Optimiser conservation/capacity/mode, two periods | Pass, bounded | Actual HiGHS run; independent maximum balance residual zero |
| Planner DST overlap/duration | Fail | R13; spring/autumn energy and repeated-hour validation |
| Planning report workbook/end-to-end scenarios | Partial, source | Equations and bounded solver checked; full report/scenario combinations not executed |
| Replay/live parity and as-of future-data exclusion | Partial, source | Existing questions retained; no exhaustive chronological replay fixture |
| Leap-day and long gaps spanning tariff/day boundaries | Not checked independently | Existing date tests do not certify interval attribution/billing through gaps |
| Configuration validation | Partial | Existing tests and source; empty planning schedules/unknown-key policy remain documented questions |
| Dependency health/platform/license | Partial | Installed requirement consistency and direct metadata/wheels checked; no Docker build, full transitive audit or vulnerability scan |
| Dashboard authorization/exposure | Partial, source | No app authentication; intended deployment boundary and actual exposure unknown |
| Tariff predicate duplication / legacy adapters / writer ownership | Assessed | Ranked consolidation recommendations, preserving intentional semantics |
| Research/backfill/manual tariff scripts | Partial, source | Ownership/side effects and existing findings considered; not all scripts executed |
| Full test-suite collection | Fail | Missing migration module (R18) |
| Documented project test subset | Pass within subset | 137 passed, one browser module skipped; excludes missing migration test |
| Standalone warning browser tests | Pass in separate environment | Five passed using system Python/Playwright; not a full project-suite pass |

## Follow-up priorities for verification gaps

1. Add regression checks for confirmed correctness/recovery failures before implementing changes.
2. Finish a frozen-clock browser oracle covering every chart series/point, all-null states, forecast-only history and actual/forecast date boundaries. Independently verify desktop/mobile controls and legend/zoom interactions, not only option dictionaries.
3. Exercise simultaneous collection/recalculation/migration failures with isolated real processes and compare raw versus rollup invariants after restart.
4. Verify chronological live/replay forecast parity and planning interval semantics with DST/partial-day/partial-panel data.
5. Build the target Docker image and document deployment trust/access policy; use hardware/provider recordings or a simulator to complete collection protocol/failure coverage.

These are outstanding checks, not passing findings. Completion of the written review does not close them.
