# Review fixes: R05-R19

Implemented 2026-10-15 after R01-R04. These are working-tree changes, tested against temporary databases and an isolated browser service. Production `solar.db` and hardware were not used. Original independent reports and reproduction outputs remain historical.

## Changes and verification

| IDs | Correction | Targeted evidence |
| --- | --- | --- |
| R05 | Raw tariff update and hourly replacement share one transaction | Failure after rollup insertion leaves both rates/costs at 0.1; success commits both at 0.5 |
| R06 | Store each nullable telemetry/rate field's valid count; rebuild older rollups under readiness marker v3 | 4/NULL → 4, 4/0 → 2, all-null → missing; SOC/rates/availability and incremental/rebuilt results checked; interrupted v2 upgrade retries |
| R07 | Omit power/battery/SOC Total rows; render missing aggregate cells blank | Actual browser tables retain energy totals, omit power/battery totals and contain no `nan` cells |
| R08 | Integer-second bounds use ceil(start)/floor(end) | Final selected-day second retained; next midnight excluded across seven download timesteps, forecasts on/off, and telemetry |
| R09/R10 | Configured-local collection dates and selectors accepting any configured panel | Kolkata early-local collection yields 2 kWh; panel-2-only latest/BoD selection works, ignoring newer unconfigured panels |
| R11 | Exact target alignment; interpolate instantaneous W within source coverage only | 10:00/10:30/11:00 retain 1000/2000/3000 W and initial 500 Wh; differing 10:15 target gets 1500 W; no extrapolation |
| R12 | Rebuild whole overlapping UTC hours | Kathmandu/Kolkata boundary sums 3/3 and counts 2/2; neighboring samples and repeat rebuild preserved; Dublin also checked |
| R13 | UTC durations/overlap/steps, distinct fold keys, fold-aware local output grouping | Both DST transitions give 1 kWh per elapsed hour; database loader, scenarios and real HiGHS four-hour plans checked |
| R14 | Exclude open target hours using UTC hour floor of explicit/default as-of | Previous hour eligible, current hour excluded; later current/future observations do not affect frozen evaluation |
| R15 | Terminate/reap already-launched children if startup fails | Mocked first/second launch failures on initial start and restart; original error propagates |
| R16 | SQL hourly actual means, narrow forecast projection, bounded scalar training before merge | Scalar and prediction parity against prior full-history training; future actuals excluded; scale measurements below |
| R17 | Date-only hour-range arrows explicitly move one calendar day | Symmetric navigation for 1/6/23/24/25 hours on both DST dates; desktop/mobile tooltips describe day steps; future cap retained |
| R18 | Replace obsolete missing-module test with current opener contract | Legacy values/primary keys/availability rename, readiness and repeat opening verified; ordinary pytest collection restored |
| R19 | Responsive custom-builder controls/plot/legend; warning below modal dialogs | Solar+Load, refresh, legend toggle and bottom Close pass with stale banner at 1440×1000, 390×844 and 360×800 |

Server regressions: [test_remaining_review_fixes.py](../../tests/test_remaining_review_fixes.py), plus current [migration](../../tests/test_database_0_1_0_migration.py) and [grid-statistics](../../tests/test_grid_power_rollup.py) tests. **Full project suite: 228 passed, 1 skipped**, 2119 warnings, 36.16s. The skipped Playwright module lacks its dependency in the project interpreter. A subsequently strengthened scalar/prediction-parity regression also passed independently.

Separate system-Python warning/browser suite: **5 passed**, 11.88s. Full-application Chrome checks used a fresh temporary copy of the [isolated fixture launcher](system-review-evidence/browser/server.py), with fake live readings and mocked administrative actions. Mobile Variables/plot widths are now 310px at 390px and 280px at 360px; Solar toggling leaves only Load. No page errors were observed. Screenshots were inspected. The pre-existing wider Recent-page layout is outside this custom-dialog repair.

## Forecast preparation measurements

Local single-run measurements, Python 3.12.10. Old code: revision `6f5fc65`; new: this working tree. The original one-year fixture contains 525,600 minute actuals and 420,480 forecast rows. Three years repeats its records with 365/730-day epoch offsets: 1,576,800 actuals and 1,261,440 forecasts. Raw history is retained.

| History / implementation | Load inputs | Calculate one target day | Retained frames | Peak process working set |
| --- | ---: | ---: | ---: | ---: |
| 1 year / old | 2.53s | 15.72s | 250.4 MiB | 394.5 MiB |
| 1 year / new | 0.94s | 0.16s | 57.9 MiB | 195.1 MiB |
| 3 years / old | 9.03s | 135.33s | 751.1 MiB | 1047.2 MiB |
| 3 years / new | 2.89s | 0.33s | 173.6 MiB | 451.6 MiB |

Each measurement uses a fresh process, `_load_inputs(path)`, then `calculate_adjusted_forecast(frames, latest_collection, 'Europe/Dublin')`. Peak memory is Windows `GetProcessMemoryInfo.PeakWorkingSetSize`, including interpreter/imports and loading/calculation; retained frame sizes use pandas `memory_usage(deep=True)`. The fixed-value fixture produces the same single 2000 W target in all runs; richer training/prediction parity is covered by regression tests. These timings exclude network, writes, Wh reconstruction and additional target days. They establish a preparation improvement, not a production cadence guarantee. Forecast history still scales with database age; distribution history is intentionally not truncated.

## Deployment and remaining boundaries

- First startup on databases lacking `hourly_rollup_initialized_v3` rebuilds hourly history once under a write lock. Use the managed launcher; interrupted work retries. See the [runbook](../operations/runbook.md).
- Hour navigation uses calendar days; a datetime-based anchor would be a separate UI change. Forecast interpolation applies to instantaneous W; cumulative energy retains its documented integration convention.
- Negative generation bounds, planning/dashboard interval meaning, replay parity, availability download last-versus-mean, and deployment access policy remain explicit decisions in [findings](findings.md).
- Real provider/device behavior, target Docker installation, actual process kills, sustained concurrent SQLite pressure and exhaustive browser combinations remain unverified. Optional consolidation proposals remain proposals.
