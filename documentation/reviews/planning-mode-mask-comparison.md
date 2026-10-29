# Single-solve mode-mask comparison, 2026-10-29

This dated investigation describes snapshots before the subsequent command/response limit correction. The final [Python-list follow-up](#python-list-follow-up-retained) records the last snapshot benchmarked here. Current response formulas encode user energy bounds in command SOC stops, reject unsafe passive responses and constrain export-safe shared discharge rates; see the [current planning contract](../implementation/planning.md). These timing/economic comparisons have not been rerun for that correction. Earlier tables retain the preceding mode-mask snapshot for comparison.

The retained implementation returns to one solve with three shared Boolean mode flags. It replaces the three-worker trial, keeps the first-step calculations straightforward, and avoids the earlier NumPy stack/broadcast/reduce operations. All 21 production-grid inputs completed within five seconds, with comparable full-horizon economics to the earlier single-solve port model. The finer grid remains sensitive: two of nineteen inputs timed out.

## Code structure and retained contracts

`next_mode` has three Boolean entries: self-consumption, charge and discharge. Their sum is one. Named masks control the corresponding rate bounds and response equations; the returned OperatingMode comes from the selected flag. This is one shared mode and command across all nine scenarios, with scenario-specific achieved energy and independent later decisions.

Each scenario's first-step response uses ordinary arithmetic and Python min/max. For example, passive charge is `min(pv - pv_load, charge_limit, charge_room)`. Numeric input matrices use ordinary list comprehensions. Battery conservation is a loop over periods referring directly to the preceding battery state. Vector arithmetic remains for common physical constraints. A simple Boolean array selects cells with a binding battery-export cap.

Two short helpers retain the necessary linear-programme encoding:

- `_when(mask, value, target, upper, constraints)` activates an equality when a mode is selected, using the physical bound to relax it otherwise. Multiplying two unknown solver expressions directly would be nonlinear.
- `_minimum(requested, ceiling, upper, constraints)` implements an exact scalar minimum, adding a Boolean branch only when the request can cross the numeric ceiling. Upper bounds alone would permit economically convenient under-delivery.

The existing `_energy_until_soc` handles exact min/max clipping at SOC stops. Scoring, projection construction, probabilities, reserve/terminal valuation, physical/configured limits, report allocation and the public SimulationResult contract are retained. Mode remains explicit when a reached SOC stop makes achieved battery transfer zero. No mode process pool, candidate selection or worker-only test scaffolding remains.

## Benchmark scope

Compared with the saved `current` results from the [earlier single-solve port comparison](planning-port-comparison.md), using identical configuration and synthetic input preparation. This is not a database replay or hardware test.

Production uses the current 28 visible intervals plus six hidden intervals, the same nineteen sensitivity inputs and separate fourfold-solar/load stresses. The finer benchmark uses 43 visible intervals plus six hidden intervals and nineteen inputs. Each solve contains all nine scenarios. Each input ran once; baseline calibration was not repeated. Workers exceeding five seconds were killed and recorded N/A, with no retries. Controller startup/imports/input preparation are excluded; model construction, compilation, solving, extraction and validation are included.

Environment: Windows 10.0.26200, Python 3.11.8, CVXPY 1.7.5, HiGHS 1.15.1, 20 logical CPUs. BLAS/OpenMP limits are one and the single HiGHS solver uses automatic settings, matching the earlier single-solve benchmark. No tests or other benchmarks ran concurrently. The temporary CVXPY-first import workaround was retained; application startup code was unchanged.

## Results

| Measure | Earlier single solve | Three-worker trial | Retained simple mode masks |
| --- | ---: | ---: | ---: |
| Production inputs completed | 21/21 | 17/21 | 21/21 |
| Production completed median | 2.034 s | 4.505 s | 1.830 s |
| Production completed range | 0.779-3.637 s | 3.347-4.992 s | 0.944-3.219 s |
| Production baseline | 2.725 s | 4.089 s | 1.531 s |
| Finer-grid inputs completed | 16/19 | 2/19 | 17/19 |
| Finer-grid completed median | 3.330 s | 4.120 s | 2.939 s |

Against the earlier single solve, median per-input production runtime ratio was 0.945, about 5.5% faster. These single-run observations do not establish a durable performance ranking. Finer-grid medians exclude different timed-out inputs and must not be read as paired rankings. The retained model timed out on `load_0.95` and `load_1.05`; those results remain N/A. It completed the old finer-grid timeout inputs baseline, battery 4 kWh and battery 8.1 kWh, but no paired economic result exists for those previously timed-out inputs.

| Accuracy measure against earlier single solve | Production | Finer grid |
| --- | ---: | ---: |
| Completed paired inputs, nine scenarios each | 21 | 14 |
| Maximum absolute weighted full cost difference | EUR 0.003311 | EUR 0.001194 |
| Maximum absolute individual scenario full cost difference | EUR 0.012850 | EUR 0.015238 |
| Maximum absolute weighted full score difference | 0.000474 | 0.000367 |
| Full-horizon final battery energy difference | Numerical rounding only | Numerical rounding only |

Every completed run passed independent physical/billing checks and returned optimal status. The probability-weighted score reconstructed from visible and hidden reports matched the native objective within 0.000001. Code review also checked that the scoring, projection and SOC-clipping functions were unchanged. Existing numerical regression cases cover shared commands, saturation, asymmetric AC/battery ratings, losses and restrictive export schedules.

Visible-only reports can still differ substantially for protection preference zero: the largest production visible-cost difference was EUR 1.496682, with a 9.180974 kWh visible-end energy difference; finer-grid differences reached EUR 2.208817 and 13.549360 kWh. Near-equivalent full-horizon solutions move charging between visible and hidden periods, as in earlier comparisons. Small full-score differences also reflect solver optimality tolerances. No tie-break objective was introduced to force identical intermediate schedules, and no economic equivalence is claimed for timed-out cases. Firmware and subinterval response remain unverified model assumptions.

## Per-input runtime

| Input | Production earlier s | Production retained s | Finer earlier s | Finer retained s |
| --- | ---: | ---: | ---: | ---: |
| baseline | 2.725 | 1.531 | N/A, timeout | 2.833 |
| battery_4 | 2.551 | 1.776 | N/A, timeout | 2.471 |
| battery_7.9 | 2.624 | 1.698 | 4.844 | 3.268 |
| battery_8.1 | 2.020 | 1.909 | N/A, timeout | 3.782 |
| battery_14 | 1.679 | 1.443 | 1.580 | 2.213 |
| solar_0.75 | 3.637 | 1.800 | 3.647 | 3.217 |
| solar_0.95 | 2.325 | 1.684 | 3.232 | 2.962 |
| solar_1.05 | 2.034 | 2.191 | 4.448 | 2.771 |
| solar_1.25 | 1.980 | 1.615 | 3.383 | 3.077 |
| load_0.75 | 2.177 | 2.335 | 2.378 | 2.888 |
| load_0.95 | 1.756 | 3.219 | 3.163 | N/A, timeout |
| load_1.05 | 2.506 | 1.945 | 2.444 | N/A, timeout |
| load_1.25 | 1.487 | 2.018 | 3.872 | 3.494 |
| preference_0 | 0.779 | 0.944 | 0.836 | 1.782 |
| preference_50 | 1.983 | 1.830 | 3.276 | 3.148 |
| reserve_2 | 1.503 | 1.900 | 4.746 | 2.939 |
| reserve_6 | 1.641 | 1.619 | 1.721 | 2.120 |
| tariff_flat | 2.650 | 2.022 | 4.763 | 3.281 |
| tariff_peak_1.5 | 2.238 | 2.188 | 4.136 | 2.658 |
| solar_4 | 1.512 | 1.772 | Not tested | Not tested |
| load_4 | 2.624 | 2.988 | Not tested | Not tested |

## Reproduction and provenance

Run `python -m solar_model.tools.benchmark_planning --grid production --output production.json`, add separate `--cases solar_4 load_4` for stresses, and use `--grid historical` for the finer suite. The controller enforces the five-second cutoff and saves each case as it finishes.

- Earlier single-solve source SHA-256: `d9d5e588db87478dd8035c0a3338279a843f5f5a97c36e008add737772763595`.
- Retained source SHA-256: `6d85af10a370779a4bee0c5a943e41b3e97bac6c310644224afc66bc1f0969d2`.
- Unchanged configuration SHA-256: `c57bb64435b71a3a3f0c70c7a8c88e176c62ebff41c51282d72472f303ec990e`.

Local ignored evidence is under `documentation/reviews/system-review-evidence/port-model-comparison/`: `simple-mode-baseline.json`, `simple-mode-production.json`, `simple-mode-historical.json`, corresponding logs and `simple_mode_optimisation.py`. Earlier results remain in `initial.json` and `production-extra.json`; the three-worker trial is retained as historical evidence. These measurements apply to the stated source/configuration snapshots and do not establish annual savings or a universal runtime bound.

## Python-list follow-up (retained)

The final refactor removes direct NumPy imports from `planning/optimisation.py` and `planning/system.py`. Numeric inputs are Python lists indexed `[period][scenario]`. Each period's physical constraints use scalar equipment limits and one-dimensional scenario lists; per-scenario costs use one-dimensional rate lists over time. These lists pass directly into CVXPY, including `cp.multiply`. This also avoids CVXPY's nested-list orientation ambiguity: a nested-list constant is interpreted as columns and emits a warning. CVXPY quantity matrices remain the solver variables.

Extraction converts native solver values to lists, performs numerical clipping and protection reporting with scalar min/max, and passes matching-length iterables to the pure-Python report allocator. That allocator loops over periods and returns the same eleven route columns as Python lists. Scoring, shared mode/command behaviour, physical/configured limits and public reports are unchanged. Common constraints are now constructed per period rather than using numeric array arithmetic and Boolean array indexing.

Both benchmark suites were repeated once using identical configuration/inputs, the same five-second cutoff, no concurrent tests and the environment described above. Comparisons below use the immediately preceding `simple-mode-*` results, not the older port or worker models.

| Measure | Previous numeric-array mode masks | Retained Python lists |
| --- | ---: | ---: |
| Production inputs completed | 21/21 | 21/21 |
| Production completed median | 1.830 s | 2.092 s |
| Production completed range | 0.944-3.219 s | 1.209-2.834 s |
| Production baseline | 1.531 s | 1.740 s |
| Finer-grid inputs completed | 17/19 | 19/19 |
| Finer-grid completed median | 2.939 s | 3.043 s |
| Finer-grid completed range | 1.540-3.782 s | 1.540-4.535 s |

All forty inputs completed below five seconds. Production median paired runtime ratio was 1.101, about 10% slower. The extra per-period constraint construction/compilation is part of the measured call. Finer-grid paired ratio across seventeen comparable completed inputs was 0.996, effectively unchanged; medians include different survivor sets. The two previously timed-out finer-grid load inputs completed in 3.122 and 3.530 seconds. These single runs do not establish a universal runtime bound or durable performance ranking.

All completed runs returned optimal status and passed physical/billing validation, including report score agreement with the native objective within 0.000001. Across all nine scenarios, maximum absolute weighted full-cost difference from the preceding mode-mask version was EUR 0.003110 for production and EUR 0.001413 for the finer grid. Maximum individual-scenario full-cost differences were EUR 0.012850 and EUR 0.010596. Maximum weighted full-score differences were 0.000503 and 0.000341. Full-horizon final battery energies matched within rounding. As before, nearly equivalent schedules at zero protection preference can differ in visible-only timing; the largest production visible-cost difference was EUR 1.496682. No new tie-break or optimisation goal was introduced.

The numerical tests also verify list-valued route outputs and independent conservation using the original mixed charge/discharge/PV example. Scoring, timestep and SOC-clipping helpers remain unchanged. The prior finer-grid timeout finding is removed from current findings because all nineteen cases passed on this snapshot; the older observations remain above as history.

Current optimisation SHA-256: `0a3c37f3d1a33b3cd901284dd7d5ecee763665365a44b2c140642ac07f28c338`. Current physical/report helper SHA-256: `8226c30ebf5ca2ade7bd5aa8e98fdee3867376ec74b95e8b6611de429f7dd5b8`. Configuration hash is unchanged. Local ignored evidence in the same directory includes `python-lists-production.json`, `python-lists-historical.json`, their logs and both `python_lists_*.py` source snapshots.
