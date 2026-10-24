# Independent initial assessment: data correctness

Reviewed 2026-10-15. **Review artifact complete; system verification partial. No fixes applied.** This initial assessment was formed without consulting other new reviewers' reports. Existing documentation/findings were read as context and checked where explicitly stated below.

Material gaps: no real hardware/provider response capture; no production database opened or copied; no browser verification; no exhaustive migration interruption, forecast replay parity, tariff experiment, or model-training run. Passing synthetic checks do not cover these gaps.

## Baseline and evidence

- Source HEAD: `3932b02f6de46bc74dc7392da0022726cf7af1d5`. Initial working changes: modified `documentation/index.md`; untracked review plan and `solar_model/tools/evaluate_load.py`. All preserved. Other reviewers' concurrent artifacts are outside this assessment.
- Verification interpreter: `.venv\Scripts\python.exe`, Python 3.12.10; pandas 2.3.3; SQLAlchemy 2.0.52; CVXPY 1.9.3. Installed solvers include HIGHS; the actual HIGHS solver was used. Initial system-Python inspection found 3.11.8/CVXPY 1.7.5 without HIGHS; that is **not** a finding about the project venv.
- Reproduction script: check_data.py (historical temporary artifact: `C:/Users/glees/AppData/Local/Temp/solar-review-data-20261015/check_data.py`; no longer retained).
- Machine-readable results: results.json (historical temporary artifact: `C:/Users/glees/AppData/Local/Temp/solar-review-data-20261015/results.json`; no longer retained).
- Solver routes: solver_plan.csv (historical temporary artifact: `C:/Users/glees/AppData/Local/Temp/solar-review-data-20261015/solver_plan.csv`; no longer retained).
- All fixture databases are in that same temporary directory. The harness recreates only its explicitly named fixture files. Run from repository root: `.venv\Scripts\python.exe C:\Users\glees\AppData\Local\Temp\solar-review-data-20261015\check_data.py`.
- Independent expectations use arithmetic, physical-unit contracts, and controlled inputs; they do not call application aggregation functions to construct the expected results. Actual outputs do use the reviewed functions. The NaN persistence probe substitutes only the input loader, then executes the actual adjustment/write path.
- `.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_database_0_1_0_migration.py`: **137 passed, 1 skipped, 1769 warnings, 14.73 s**. This is the documented subset, not a passing full suite. `.venv\Scripts\python.exe -m pytest -q --collect-only` still fails on absent `migrate_database_0_1_0`; 137 other tests collected. A browser module is skipped. Deprecation/null-equality warnings remain.

## Confirmed findings

### DATA-01 — Missing completed actual hour aborts adjustment (High, high confidence)

Category: correctness/reliability. Affects `forecast/operational_model.py::_today_residual`, `calculate_adjusted_forecast`, and adjusted persistence.

The official forecast is left-joined to observations. `_today_residual` admits rows with missing observed power, producing a NaN residual. With valid historical hour distributions, that NaN propagates into every predicted panel, including zero-share panels. `ScaledInteger` then raises `ValueError: cannot convert float NaN to integer`; the adjusted transaction fails. Raw forecast rows survive. An ordinary collection outage can therefore suppress later adjusted forecasts throughout the rest of that day.

Reproduction: official targets 09:00 and 10:00 have 1000 W; prior three days have actuals 0.5/1/1.5 kW at both hours. Today's 09:00 observation is missing; calculate at 10:15. With no calibration training, expected fallback at 10:00 is 1000 W with zero usable residual observations. Actual result is NaN for all panels; writing it fails. Harness asserts both raw rows survive and no adjusted watts commit. Evidence key: `missing_actual_poison`.

Recommended correction: exclude non-finite observed/baseline/residual inputs before weighting; use neutral residual when no usable observations remain. Keep raw/prediction atomicity. Scope is the production inference helper plus regression coverage; avoid silently replacing missing actuals with zero.

Acceptance: this fixture writes finite 1000 W for panel 1, zero for unused panels, and coherent Wh; a mixed valid/missing history uses only valid observations; fully observed history retains its expected output. Tolerance 0.0001 W after storage.

### DATA-02 — Hourly grid telemetry cancels import/export before splitting (High, high confidence)

Category: correctness. Affects `database.py::_add_hourly_sample` and `solar_model/storage/queries/analysis.py` telemetry calculation; also retained `average_hourly_rollup_telemetry`.

Hourly storage retains signed grid-power sums. Multi-day Analysis/default hourly telemetry splits the sign of the already-netted sum. Alternating import and export disappear from both magnitudes. This is distinct from meter energy; no assertion is made that energy import/export totals fail.

Reproduction: each of two days has 10:00 grid +2 kW and 10:01 grid -2 kW. Independent per-sample magnitude means are import 1 kW and export 1 kW. Active `load_chart_data(..., sample_resolution='hour')` returns 0 and 0. Evidence: `telemetry.db`, `rollup_telemetry`.

Recommended correction: preserve separate positive/negative grid-power sums in the rollup or calculate those means from bounded raw samples. Historical rollups need rebuilding if the schema changes. Preserve the single-day minute route's meaning.

Acceptance: both means are 1 kW in hourly/multi-day Analysis and custom-chart datasets (tolerance 1e-7 kW), including all-import, all-export, tariff-split hours and unequal sign magnitudes. Browser rendering remains for website verification.

### DATA-03 — Missing telemetry is counted as zero in rollup means (Medium, high confidence)

Category: correctness. Affects hourly schema/count contract and the same telemetry readers as DATA-02.

Each hour has one total sample count, but independent nullable measurements have different valid counts. Readers divide a field's non-null sum by the count of all samples whenever that sum is non-null. Thus a missing field lowers a displayed mean as if it were observed zero. The minute route counts valid values, so switching date ranges changes semantics.

Reproduction: in the DATA-02 two-day fixture, solar powers are 4 kW and NULL each day. Mean known power is 4 kW; hourly chart result is 2 kW. Evidence: `rollup_telemetry`. Existing docs acknowledge varying missingness handling; this probe establishes a concrete active output error.

Recommended correction: per-field valid counts (and historical rebuild), or bounded raw telemetry queries. Migration/backfill scope should be assessed together with DATA-02.

Acceptance: 4/NULL averages 4, 4/0 averages 2, all-null remains missing; fields with different null patterns are independent. Raw/hourly means agree for otherwise equivalent fixture selections within 1e-7 kW or native level units.

### DATA-04 — Subhour forecast points overwrite one another (Medium, high confidence)

Category: correctness. Affects `forecast/operational_model.py::calculate_adjusted_forecast` mapping of predictions through `fields_by_hour`.

Forecast persistence accepts exact target timestamps, and energy integration uses the time to the next point. The calculator nevertheless maps every source point in an hour to the last source point's adjusted watts. This changes the curve even with identity calibration and no residual.

Reproduction: one official/destination snapshot at 10:00, 10:30, 11:00 with 1000, 2000, 3000 W, no actuals/training. Expected identity output 1000/2000/3000; actual 2000/2000/3000 W. First half-hour energy becomes 1 kWh instead of 0.5 kWh. Evidence: `subhour_targets`.

Recommended correction: match exact timestamps, or explicitly resample power to a documented target grid before mapping. Do not use arbitrary last-row replacement. Actual provider cadence was not checked; severity is bounded by occurrence of subhour targets.

Acceptance: identity curve preserves all three values and 0.5-hour energy at the first point; test sunrise/sunset and differing source/destination target grids. Tolerance 0.0001 W after storage.

### DATA-05 — Non-hour timezone rebuild corrupts boundary rollups (Medium, high confidence)

Category: correctness/reliability. Affects `database.py::rebuild_hourly_rollup`, `_insert_hourly_rollup`, and callers such as economics recalculation.

Deletion compares UTC hour starts to exact local-midnight epochs, while insertion filters raw observations by those epochs and uses `OR IGNORE`. For Asia/Kathmandu, local midnight is 18:15 UTC: the opening 18:00 bucket is not deleted, so its stale values block the replacement; the closing 18:00 bucket is deleted but recreated without following-day observations. This affects both the selected day and its neighbour.

Reproduction: one observation at 18:05 and 18:20 UTC on January 1 and 2; initial PV is 1 kW each. Change selected local January 2's two observations to 2 kW and rebuild only that local day. Both UTC-hour sums should be 3 kW, counts 2/2. Actual sums are 2/2, counts 2/1. Evidence: `nonhour_rebuild.db`, `nonhour_rebuild`.

Recommended correction: expand deletion and raw reaggregation to complete overlapping UTC-hour boundaries, preserving outside-date samples in those buckets. Same-band conflicts and neighbour integrity require testing.

Acceptance: sums 3/3 and counts 2/2; repeated rebuild is identical; outside-range observations survive; verify quarter-hour/half-hour offsets and ordinary Dublin dates. Counts exact; storage sums within 0.0001 unit.

### DATA-06 — Planning interval arithmetic misallocates energy across DST (Medium, high confidence)

Category: correctness. Affects `energy_plan_simulation.py::ForecastData.sum`, `_sum_intervals`, validation, and local-wall-time step construction; callers build ZoneInfo local datetimes.

Python subtraction/comparison between datetimes carrying the same ZoneInfo uses wall time. A repeated-hour interval can compare equal despite one hour of UTC elapsed time. Proportional integration also uses incorrect denominators across offset changes.

Reproduction in Europe/Dublin: October 25 00:00 to 03:00 is four real hours; uniformly distributed 4 kWh should yield 1 kWh in each real hour. The first real hour returns 1.333333 kWh. Querying 01:00 fold=0 through 01:00 fold=1 raises `forecast end_time must be after start_time`. March 29 00:00 to 05:00 is four real hours; a real one-hour subinterval returns 0.8 instead of 1 kWh. Evidence: `dst_fold`.

Recommended correction: validate/compare/integrate on UTC instants and advance simulation intervals by elapsed UTC time; convert to configured local timezone only for tariffs and labels. This requires coordinated planner/scenario changes, preserving both repeated local hours.

Acceptance: all three cases yield 1 kWh within 1e-9; a 24 elapsed-hour projection has exactly the requested elapsed duration and power-energy limits through both DST transitions. Retain distinguishable repeated-hour rows and tariff lookup by local instant. Existing fixed-24-slot dashboard behavior is a separate issue.

### DATA-07 — Evaluator includes the current incomplete actual hour (Medium, high confidence)

Category: correctness. Affects `forecast/evaluate_forecasts.py::forecast_performance` and performance claims.

The function/module describe completed-hour comparisons, but impose no actual completion cutoff. A current-hour power sample makes that hour eligible immediately. MAE/RMSE can therefore change as the hour fills and compare predictions to an incomplete, potentially unrepresentative mean. Historical gaps likewise receive no coverage label; that broader coverage policy remains a decision.

Reproduction: forecast issued one hour before the current UTC hour, target current hour, one current-hour actual sample. Expected zero completed pairs; actual one pair. Evidence: `evaluation.db`, `evaluation_open_hour` (records execution clock).

Recommended correction: explicit evaluation as-of/completed-hour cutoff, with a stated policy for historical sample coverage. Preserve snapshot <= target exclusion.

Acceptance: current-hour target excluded until hour end; previous completed hour included; frozen `as_of` reproduces identical pairs despite later database samples. Add partial-coverage metadata or exclusion according to the agreed policy. Counts exact.

### DATA-08 — Negative adjusted solar is concretely reproducible (Medium, high confidence in behavior; physical-bound policy open)

Category: correctness/model policy. Existing findings list this as an unresolved question; this review upgrades its evidence, not the decision about a correction.

`_prediction_fields` with raw 100 W, scalar 1, sigma 1 kW, residual -2, strength 0.5 returns **-900 W** for the active panel. A daylight adverse residual can produce physically impossible negative generation and decreasing cumulative Wh. Evidence: `negative_adjusted`. The input parameters are deterministic model-state inputs; no assertion is made about frequency in the user's actual history.

Recommendation: decide and document physical output bounds, then enforce nonnegative power before integration and preserve site/panel allocation consistency. No implementation decision was applied during review.

Proposed acceptance if nonnegative generation is required: this case yields 0 W, never negative Wh increments; zero-raw night remains zero; positive curves remain unchanged. Storage tolerance 0.0001 W/Wh. If negative model scores are intentionally retained, expose them distinctly from physical energy and keep them out of physical planning flows.

## Coverage and positive checks

All interaction/browser columns below are **Not applicable** to this server-side reviewer; no row establishes rendered correctness.

| Area/scenario | Values/behavior status | Evidence / limits |
| --- | --- | --- |
| Signed u32/s32 wire decode, short register response | Pass | Harness `wire`; no protocol/hardware/sentinel validation |
| Electrical fixed-point ORM/raw boundary | Pass | 2.5 kW persists as 25000 and reads as 2.5 |
| First meter observation and counter reset | Pass | NULL interval energy; reset baseline becomes 1 kWh |
| Normal interval delta, PV trapezoid, costs | Pass | 0.03 kWh plant delta; 0.025 kWh string energy; import .006, export .001, net .005 |
| Duplicate timestamp transaction and restart baseline | Pass | Duplicate rejects; raw count and rollup count remain 2; previous state unchanged; persisted latest counter reloaded |
| Missing actuals and adjustment persistence | Fail | DATA-01; raw rows preserved after rejected adjusted write |
| Forecast exact target preservation | Fail | DATA-04; subhour synthetic timestamps |
| Raw vintage immutability under adjustment | Partial | Writer ownership checked; NaN failure retains raw; successful end-to-end multi-panel curve not comprehensively exercised |
| Negative physical adjusted output | Fail / policy open | DATA-08 |
| Hourly grid signs and independent null patterns | Fail | DATA-02/03; active chart SQL, no browser |
| Non-hour local-day rollup rebuild | Fail | DATA-05 |
| Optimiser solar/load/battery/cost equations | Pass, bounded | Two one-hour periods, solar 4/0 kWh, load 1/3, start battery 2, capacity 10, charge efficiency .9, discharge .8; HIGHS. Independent maximum residual 0; bounds and mutually exclusive charging/discharging checked |
| ForecastData DST overlap/integration | Fail | DATA-06; spring and autumn Europe/Dublin |
| Evaluator completion boundary | Fail | DATA-07 |
| Schema rename/add/idempotence | Partial | Existing subset covers raw forecast rename and battery column migration on temporary DBs; missing standalone migration module blocks full suite |
| Configured timezone vs host; solar start/end interval convention | Partial | Source traced and existing documented conflicts remain; not independently re-probed here |
| Tariff transitions, effective dates, calendar buckets | Partial | Source traced; normal constant-rate arithmetic checked; subset provides broader examples; no independent complete transition fixture |
| Provider partial arrays, retries, unavailable sentinels | Partial | Sequential partial-snapshot/error paths inspected; no complete deterministic request-failure matrix or live provider calls |
| Planning as-of selection and profiles | Partial | Cutoff, raw/adjusted fallback, profile and scenario source inspected; no end-to-end workbook fixture |
| Historical replay/live parity | Not checked dynamically | Source confirms replay only handles collection-local target day while live loops target days; existing finding remains |
| Research training/backtesting and tariff scripts | Partial | Inspected separation, units and known side effects; not executed; no model-quality endorsement |
| Leap day, cumulative gaps spanning tariff/day boundaries | Not checked independently | Documented attribution policy remains; no claim of billing accuracy through gaps |
| Production history spot checks / scale | Not checked | No production database touched |

## Consolidation recommendations

1. **Hourly telemetry contract**: preserve the required sufficient statistics (per-field count and split grid magnitudes), then make raw/hourly readers share explicit mean semantics. DATA-02/03 show concrete divergence. Migration/rebuild risk is substantial; add fixtures before consolidation.
2. **UTC interval primitives for planning**: centralise elapsed-duration/overlap operations used by `ForecastData`, as-of construction, scenario splitting and projection steps, while retaining configured-local tariff lookup. DATA-06 demonstrates why merely sharing wall-clock helpers would preserve the bug.
3. **Forecast target alignment and validity**: keep exact target instants, finite observed values and fallback rules explicit in a shared production/replay contract. Preserve intentional differences in research models and forecast vintage selection; do not unify them by name alone.

The first recommended fixes are DATA-01 and DATA-02, followed by agreement on telemetry statistics, timezone-safe rebuild/interval handling, and forecast alignment/evaluation boundaries. These are review recommendations only. Existing documented planning interval and host-timezone questions require their own acceptance decisions and should remain visible in consolidation.
