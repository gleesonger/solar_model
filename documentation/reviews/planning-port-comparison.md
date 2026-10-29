# Planning port-model comparison, 2026-10-29

The system-port rewrite produced comparable full-horizon economics to the previous route model on the completed synthetic comparisons. All 21 current-production-grid inputs completed within five seconds and passed physical checks. The finer historical grid remains a performance limitation: three of its nineteen new-model inputs exceeded the five-second deadline.

## Method and scope

Compared the current working-tree `planning/optimisation.py` with its pre-rewrite snapshot, verified equal to Git commit `85298f7` for that file. The previous solver used the same unchanged configuration, physical-limit helper, scenario generation, scoring, dependencies and input preparation. No production database or device was accessed.

The driver reused `tools/benchmark_planning.py` input preparation and fresh-process deadline controller. Each input generated all nine low/medium/high solar/load combinations and one joint solve with a shared next action. Nineteen sensitivity inputs varied starting battery energy, solar, load, protection preference, reserve and tariffs. Both models ran on:

- The production grid at 10:05 UTC: 28 visible intervals, `[15, 10, 15, 15] + [60]*23 + [5]`.
- The historical finer benchmark grid: 43 visible intervals, `[15, 10, 15, 15] + [15]*20 + [60]*18 + [5]`.

Both grids retained the six hidden four-hour intervals. Production-grid checks additionally multiplied baseline solar and load by four, separately. Devices had an 18 kWh battery, 5 kW charge/discharge/AC ratings and 95% charge/discharge efficiencies. Input start was 2026-10-27 10:05 UTC. This is synthetic evidence, not a replay of the installation.

One run per model/input, current model first, no concurrent tests or benchmarks. Workers exceeding five seconds were killed, their result recorded N/A, and remaining model runs for that input/grid skipped. Startup/import/input preparation was excluded; timings include model construction, compilation, solve, extraction and independent validation. The comparison adds validation to the existing benchmark, so its totals are not directly interchangeable with older published timings. BLAS/OpenMP thread limits were one; HiGHS used automatic settings. Windows 10.0.26200, Python 3.11.8, CVXPY 1.7.5, HiGHS 1.15.1. A temporary `sitecustomize.py` imported CVXPY first to avoid the local native-library import-order failure; application startup code was unchanged.

## Economics and scenario comparison

| Completed paired inputs | Production grid | Historical finer grid |
| --- | ---: | ---: |
| Paired inputs, each containing nine scenarios | 20 | 16 |
| Largest absolute probability-weighted full cost difference | EUR 0.002636 | EUR 0.000990 |
| Largest absolute full cost difference in any individual scenario | EUR 0.009975 | EUR 0.015238 |
| Largest absolute weighted objective difference | 0.000789 | 0.000413 |
| Largest absolute individual scenario score difference | 0.010069 | 0.014589 |
| Visible-end battery energy difference | Numerical rounding only | See zero-preference exception below |
| Full-horizon final battery energy difference | Numerical rounding only | Numerical rounding only |

Full cost includes visible and hidden intervals; full score additionally includes protection and terminal credit. Signed costs can be negative because exports exceed imports. Objective agreement was checked against the probability-weighted sum of reported full scenario scores for each completed model run. The scoring functions and timestep construction were unchanged by the rewrite. These small differences do not establish an economic improvement; the formulations differ in first-step response and the mixed-integer solver permits a finite optimality gap.

For the production baseline, all nine scenario full costs were:

| Scenario | Previous EUR | Current EUR | Difference EUR |
| --- | ---: | ---: | ---: |
| low_low | -0.369796 | -0.369796 | 0.000000 |
| low_med | -2.156779 | -2.156779 | 0.000000 |
| low_high | -3.940951 | -3.940951 | 0.000000 |
| med_low | 2.048915 | 2.048915 | 0.000000 |
| med | 0.260112 | 0.260112 | 0.000000 |
| med_high | -1.528256 | -1.524060 | +0.004196 |
| high_low | 4.746464 | 4.746464 | 0.000000 |
| high_med | 2.714688 | 2.718849 | +0.004161 |
| high_high | 0.934488 | 0.934488 | 0.000000 |

Shared command numbers and individual interval routes need not match: corrected first-step rules and nearly equal objective values permit different selected solutions. In the baseline both models selected charge mode at 0.1 kW with an already-reached stopping SOC, producing effectively zero charging. Different unused SOC settings do not change that result. This comparison checks achieved energy and economics rather than requiring identical labels or source attribution.

**Visible-horizon exception:** on the finer grid with protection preference zero, the low-load/low-solar scenario had visible cost EUR -1.823617 previously versus EUR 0.385199 now, and visible-end battery energy 3.085526 versus 16.634886 kWh. Its full cost was identical, EUR -0.369796; full score was identical, 5.367128; both ended the full horizon at 18 kWh. The first action also had effectively zero charge in both. These are equally optimal schedules moving charging between the visible horizon and hidden lookahead. Consequently visible-only costs and intermediate trajectories are not guaranteed to match even when the complete economics match. No tie-break objective was added, preserving the requested optimisation goals.

## Physical checks

Every completed run passed independent checks over all nine scenarios, including hidden intervals: battery state/efficiency balance, solar/load conservation, tariff billing, configured battery energy bounds, battery input/output ratings, signed inverter output/absorption ratings, battery-only export limits, and exclusion of simultaneous grid import/export or battery charge/discharge. Every completed solve returned optimal status. These common checks verify the tested numerical outcomes; separate regression tests cover asymmetric battery/AC ratings, restrictive export schedules and forced-mode scenario saturation. The benchmark does not establish installed-firmware response or subinterval/transient behaviour.

## Runtime

| Measure | Previous | Current |
| --- | ---: | ---: |
| Production inputs completed | 20/21 | 21/21 |
| Production completed runtime range | 0.650-3.292 s | 0.779-3.637 s |
| Production completed median | 1.905 s | 2.034 s |
| Production median paired runtime ratio, current / previous | — | 1.137 |
| Finer-grid inputs completed | 16/16 attempted | 16/19 |
| Finer-grid completed runtime range | 0.824-3.753 s | 0.836-4.844 s |
| Finer-grid completed median | 2.346 s | 3.330 s |
| Finer-grid median paired runtime ratio, current / previous | — | 1.347 |

Production runtime was acceptable against the five-second budget in this sample, with a typical paired slowdown of about 14%. The baseline itself slowed from 1.421 to 2.725 seconds. Previous-model `solar_4` timed out; the current model completed that input in 1.512 seconds, so no paired economic result exists there. On the finer grid, the current baseline, `battery_4` and `battery_8.1` timed out and the previous-model runs were skipped as required. The finer-grid failures prevent a general claim of acceptable runtime for all supported resolutions. Single runs are not latency guarantees or a statistically controlled performance ranking.

| Input | Production previous s | Production current s | Finer previous s | Finer current s |
| --- | ---: | ---: | ---: | ---: |
| baseline | 1.421 | 2.725 | N/A, skipped | N/A, timeout |
| battery_4 | 1.970 | 2.551 | N/A, skipped | N/A, timeout |
| battery_7.9 | 2.617 | 2.624 | 3.753 | 4.844 |
| battery_8.1 | 1.927 | 2.020 | N/A, skipped | N/A, timeout |
| battery_14 | 1.194 | 1.679 | 2.206 | 1.580 |
| solar_0.75 | 2.395 | 3.637 | 2.598 | 3.647 |
| solar_0.95 | 2.267 | 2.325 | 2.538 | 3.232 |
| solar_1.05 | 1.899 | 2.034 | 2.357 | 4.448 |
| solar_1.25 | 1.778 | 1.980 | 2.334 | 3.383 |
| load_0.75 | 1.967 | 2.177 | 1.691 | 2.378 |
| load_0.95 | 1.910 | 1.756 | 3.008 | 3.163 |
| load_1.05 | 2.162 | 2.506 | 2.107 | 2.444 |
| load_1.25 | 1.363 | 1.487 | 1.977 | 3.872 |
| preference_0 | 0.650 | 0.779 | 0.824 | 0.836 |
| preference_50 | 1.204 | 1.983 | 2.958 | 3.276 |
| reserve_2 | 1.734 | 1.503 | 2.438 | 4.746 |
| reserve_6 | 1.170 | 1.641 | 1.722 | 1.721 |
| tariff_flat | 1.946 | 2.650 | 3.270 | 4.763 |
| tariff_peak_1.5 | 1.470 | 2.238 | 2.195 | 4.136 |
| solar_4 | N/A, timeout | 1.512 | Not tested | Not tested |
| load_4 | 3.292 | 2.624 | Not tested | Not tested |

## Provenance

SHA-256 of the exact files used:

- Previous optimisation snapshot: `6946f3fac7cb8d0f33a71bc64e46d588915ea0a9b8afe1ee83883f420285e867`.
- Current optimisation: `d9d5e588db87478dd8035c0a3338279a843f5f5a97c36e008add737772763595`.
- Configuration: `c57bb64435b71a3a3f0c70c7a8c88e176c62ebff41c51282d72472f303ec990e`.

Local ignored evidence is retained under `documentation/reviews/system-review-evidence/port-model-comparison/`: the comparison driver, both optimisation snapshots, per-scenario JSON results (`initial.json`, `production-extra.json`) and process logs. The driver uses the local temporary snapshot paths recorded in it; relocating it requires updating those paths. The tables above retain the findings independently of those local artifacts. Re-run the repository benchmark for later code/configuration revisions; these observations apply only to the stated snapshots.
