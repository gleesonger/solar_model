# System review: recommended work

2026-10-15. Work from the [review report](system-review-report.md). R01-R19 are implemented in the working tree; see [follow-up verification](system-review-follow-up.md). The tables retain acceptance criteria; optional consolidation and unresolved policy decisions remain proposals. Finding IDs refer to that report. Keep fixes small enough to verify independently; avoid combining broad refactoring with data corrections.

## 1. Essential correctness and recovery fixes

| Order | Work | Acceptance criteria |
| --- | --- | --- |
| 1 - completed | Filter non-finite/missing observations before forecast residual calculation (R01) | Missing 09:00 actual does not abort the 10:15 update; panel-1 prediction is finite 1000 W in the identity fixture, unused panels zero; raw data remains unchanged. Mixed valid/missing observations use only valid values. |
| 2 - completed | Publish live day state only after successful initialization (R02) | Inject one baseline failure on startup and at midnight; next healthy poll retries, publishes fresh readings and yields grid 10/5 kWh. PV state neither remains uninitialized nor carries across days. |
| 3 - completed | Make initial hourly backfill completion explicit/resumable (R03) | Interrupt after table creation and during insertion; reopen twice and recover all expected groups exactly once, preserving raw records, sums and counts. Avoid a lifetime scan on every normal open. |
| 4 - completed | Make raw economics and rollup rebuild atomic or explicitly recoverable (R05) | Inject failure between stages: both remain at old 0.1 or recovery completes both to new 0.5; errors accurately describe commit state. Preserve concurrent collection and neighboring-date data. |
| 5 - completed | Correct hourly telemetry sufficient statistics (R04/R06) | +2/-2 kW yields import/export means 1/1, 4/NULL yields 4, 4/0 yields 2, and all-null remains missing. Raw/hourly paths agree for equivalent inputs. Assess schema/backfill requirements before implementation. |
| 6 - completed | Remove misleading power/battery/SOC Total footers (R07) | No sums of levels, percentages or power are presented as meaningful totals. Either omit those footers or label an agreed average; fixture averages are 3.5 kW, 9.5 kWh available and 57.5% SOC. Define sample/time weighting and null handling. |

R06 adds per-field valid counts and a transactional v3 rebuild for older databases, superseding v2. R05 keeps tariff updates and hourly rebuilding in one transaction. Raw history is retained.

## 2. Boundaries, forecast alignment and tooling

| Work | Acceptance criteria |
| --- | --- |
| Exact export/telemetry date bounds (R08) | Next-day midnight excluded from downloads and telemetry-only charts; intended dates, final-second samples, partial ranges and DST hours retained. Assess mixed chart-query filters separately. |
| Configured-timezone summaries and panel-independent selectors (R09/R10) | Early-local collection produces the same 2 kWh on different host timezones; panel-2-only same-day vintages are selected correctly, with an explicit partial-panel policy. |
| Exact-target forecast alignment (R11) | Identity correction preserves 1000/2000/3000 W at 10:00/10:30/11:00 and the first 0.5 kWh interval. Differing source/destination grids use an explicit resampling rule. |
| Full overlapping UTC buckets for local-date rebuilds (R12) | Kathmandu boundary fixture retains sums 3/3 and counts 2/2, including neighboring-day samples; repeat rebuild is identical. Verify half-hour offsets and Dublin too. |
| UTC elapsed-time planning primitives (R13) | One real hour receives 1 kWh in both DST fixtures; fold=0 to fold=1 is valid; projection duration and power/energy constraints remain correct. Keep local tariff interpretation. |
| Completed-hour evaluation cutoff (R14) | Current incomplete hour excluded, previous completed hour included; explicit frozen as-of gives reproducible results. Decide historical sample-coverage requirements. |
| Partial supervisor startup cleanup (R15) | Fail either launch position during initial start/restart; every already-created child is terminated/reaped, and the original failure is surfaced. |
| Restore full test collection (R18) | Ordinary `python -m pytest -q` collects successfully while retaining meaningful migration/version checks. Browser dependency/skip requirements remain explicit. |
| Usable mobile custom charts/dialog actions (R19) | At 390×844 and 360×800, variable controls and chart remain usable; select Solar/Load, refresh, toggle legends and use bottom Close while a stale-data banner is displayed. |

Use per-field storage tolerance (normally 0.0001 physical unit), tighter independent arithmetic tolerances where appropriate, exact counts and explicit rounded-display checks. Reuse reproduction inputs, but write regression assertions against corrected expected behavior rather than today's defective outputs.

## 3. Decisions needed before changing semantics

- **Negative forecasts:** choose physical output bounds and panel allocation. If generation must be nonnegative, the -900 W case must become zero physical generation with nondecreasing cumulative energy; positive curves retain their expected values.
- **Hour navigation (R17), completed:** arrows move the date anchor one calendar day for hour ranges, with matching tooltips and symmetric DST-date regression coverage.
- **Access policy:** confirm LAN/reverse-proxy/public deployment expectations before selecting authentication/authorization controls. Administrative dialogs are confirmation, not access control.
- **Intervals and replay:** settle dashboard/planning interval meaning, gap attribution and live/backfill vintage parity before consolidating these paths.
- **Availability downloads:** clarify whether the state uses period-ending last value or a mean. The implementation guide explicitly specifies last for `_kwh`, but overview wording implies a mean for non-counter numeric values. Keep this documentation/contract discrepancy separate from confirmed numerical failures.

These are decisions recorded for later work; the review does not silently resolve ambiguous requirements.

## 4. Performance and consolidation

1. **Model preparation (R16), completed.** SQL computes hourly actual means, forecast reads project only needed fields, and official scalar training is selected within the 56-day window before merging. All preceding distribution history remains. Scalar parity and 1-/3-year time/memory measurements are in the follow-up; no production latency budget or cadence guarantee is claimed.
2. **Share tariff predicates.** Consolidate equivalent time/day matching between validation and economics. Verify every minute of a week, overnight rules, equal bounds, `24:00` and effective-date transitions. Keep validation and lookup policies distinct.
3. **Name data contracts before sharing mechanics.** Separate state levels from flows/counters; document raw versus rollup mean semantics and forecast snapshot/target definitions. Do not collapse stored-field downloads into chart forecast-difference semantics.
4. **Isolate legacy/reference adapters.** Identify callers before retiring old ORM/pandas paths. Retain independent expected-value coverage; parity alone can preserve shared bugs.
5. **Bound metadata reads.** Share a latest-per-variable query for device-info persistence/display, preserving history, grouping identity and unit changes. No production latency claim is made without measuring its history size.
6. **Keep justified library differences.** Retain XlsxTurbo dashboard downloads and XlsxWriter report workbooks unless a measured, feature-compatible migration has a clear benefit. Record a tested dependency/platform set; do not equate fewer libraries with better architecture.

## 5. Remaining verification work

The [coverage matrix](system-review-coverage.md) remains part of the acceptance record. Outstanding real hardware/provider checks, Docker installation, process-kill/concurrent-write recovery, full replay parity and browser edge combinations cannot be marked passed by implementing the fixes above. Recheck them in an appropriate isolated environment and update the coverage evidence when completed.
