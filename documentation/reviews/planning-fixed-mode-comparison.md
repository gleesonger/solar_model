# Fixed-mode worker comparison, 2026-10-29

Historical trial, subsequently replaced by the retained [single-solve mode-mask model](planning-mode-mask-comparison.md). Worker-specific code and tests described below apply to the trial snapshot, not the current implementation.

Three fixed-mode workers make the first-step formulation simpler and preserve full-horizon economics on completed comparisons. Fresh process startup on every call makes this implementation substantially slower on the measured Windows machine. It does not meet the five-second budget across the current production input suite and is not a performance improvement over the previous single-solve port model.

## Implementation and review

`simulate_energy_plan` starts a three-process spawn pool and submits one `_simulate_mode` call per operating mode. Each worker jointly optimises all nine scenarios with one shared next command, the existing scoring functions and the same physical/configured limits. Only the first mode is fixed; later scenario decisions remain independent.

Ordinary Python branches replace mode-selection Booleans, conditional equalities and inactive-mode response graphs. Self-consumption has numeric first-step calculations. Each forced mode constructs only its own power/SOC clipping and exact scenario saturation equations. Common system-port conservation and direction/energy limits remain unchanged. No source-route decision variables were reintroduced.

Review retained these safeguards:

- Use the same probability-weighted full-horizon objective, including hidden intervals and terminal credit, for all candidates.
- Wait for all three candidates; exclude only proven infeasibility. Other solver statuses and worker failures fail the calculation instead of selecting a partial winner.
- Exact score ties use mode enum order: self-consumption, charge, discharge. No new objective or tolerance-based tie-break was introduced.
- One HiGHS thread per worker limits nested solver threading. Every public call creates and shuts down its pool; no persistent workers or cached models were added.
- The performance controller kills the whole process tree at five seconds, preventing nested workers from continuing after a timeout. Timed-out results remain N/A even when a child printed optimality.

The numerical tests use an in-process sequential executor so existing CVXPY variable-pinning hooks remain effective. A separate real-spawn integration test checks serialization and selection against direct candidate solves. Benchmarks use real nested processes. Additional tests cover all-infeasible candidates and candidate failures.

## Benchmark method

Compared directly with the saved **single-solve port-model results** in [the previous comparison](planning-port-comparison.md), using that comparison's `current` records, not its older route-model records. Inputs, configuration, timestamps, nine scenarios, scoring and both grids are unchanged. No database or device was accessed.

- Production: the same nineteen sensitivity inputs plus `solar_4` and `load_4`, 28 visible intervals plus six hidden intervals.
- Historical finer grid: the same nineteen sensitivity inputs, 43 visible intervals plus six hidden intervals.
- Each public simulation was limited to five seconds. No timed-out input was retried. Production baseline was run once as calibration and the remaining twenty inputs once afterward.
- Controller imports/input preparation were excluded, as previously; the public call's nested process startup/shutdown was included. Both comparisons include result extraction and independent physical/billing checks. This measures the implemented public call rather than warmed worker throughput.
- Windows 10.0.26200, 20 logical CPUs, Python 3.11.8, CVXPY 1.7.5, HiGHS 1.15.1. BLAS/OpenMP limits were one. The previous single solver used automatic HiGHS settings; the new three workers each use one thread. No other tests/benchmarks ran concurrently. The same temporary CVXPY-first import workaround was used.

Reproduce current cases with:

```powershell
python -m solar_model.tools.benchmark_planning --grid production --output production.json
python -m solar_model.tools.benchmark_planning --grid production --cases solar_4 load_4 --output production-stress.json
python -m solar_model.tools.benchmark_planning --grid historical --output historical.json
```

## Accuracy

All 19 completed new-model runs passed validation, with all nine scenarios present. The best of the three native solver objectives was independently checked against the selected result's probability-weighted full reported score within 0.000001. All three native solves in each completed run returned optimal status. The scoring and timestep functions remain unchanged.

| Completed paired inputs | Production | Finer grid |
| --- | ---: | ---: |
| Input pairs, nine scenarios each | 17 | 2 |
| Maximum absolute weighted full cost difference | EUR 0.003307 | EUR 0.000610 |
| Maximum absolute individual scenario full cost difference | EUR 0.009444 | EUR 0.015238 |
| Maximum absolute weighted full score difference | 0.000425 | 0.000367 |
| Full-horizon final battery energy difference | Numerical rounding only | Numerical rounding only |

These results support comparable full-horizon economics on completed inputs, not an economic improvement or equivalence on timed-out inputs. Separate solves and their finite MIP gaps can select different nearly optimal schedules or commands.

Visible-only results are not guaranteed to match. With protection preference zero, production visible cost differed by up to EUR 0.291899 and visible-end battery energy by 1.790573 kWh; finer-grid differences reached EUR 2.208817 and 13.549360 kWh. Full economics and final energy matched for those zero-preference cases: equally optimal schedules moved charging between the visible day and hidden lookahead, as in the previous comparison. No tie-break was added to force identical intermediate trajectories.

Checks covered conservation/billing, battery energy bounds, ratings, reconstructed battery-export limits, exclusive grid/battery directions and visible/hidden results. Existing targeted numerical tests additionally cover asymmetric AC/battery ratings, forced-mode saturation and restrictive export schedules. Mathematical checks do not establish installed-firmware response or subinterval behaviour.

## Runtime result

| Measure | Previous single solve | Three fixed-mode workers |
| --- | ---: | ---: |
| Production completed | 21/21 | 17/21 |
| Production completed median | 2.034 s | 4.505 s |
| Production completed range | 0.779-3.637 s | 3.347-4.992 s |
| Production median paired runtime ratio | — | 2.250 |
| Finer-grid completed | 16/19 | 2/19 |
| Finer-grid completed median | 3.330 s | 4.120 s |
| Production baseline | 2.725 s | 4.089 s |

Completed-only medians exclude timeouts and are not worst-case bounds. The paired production ratio is the median of seventeen per-input ratios; it is about 2.25 times slower. The finer-grid completed medians describe different survivor sets and should not be treated as a paired ranking.

The production baseline's slowest native candidate solve was 1.368 seconds with about 0.2 seconds compilation, while the public call took 4.054 seconds before external validation. The previous native solve took 2.253 seconds. Smaller fixed-mode problems did solve faster in that example, but the combined startup, construction, extraction, transport and shutdown cost outweighed the native saving. This experiment does not separately measure every overhead component.

The fresh-process implementation did not meet the five-second budget and was subsequently replaced by the single-solve mode-mask model. Its source snapshot remains in local ignored evidence. A reusable prewarmed pool was considered but not implemented or measured in this trial.

### Native solving separately from startup

For the seventeen completed paired production inputs, compare the previous native solve duration with the slowest of the three new native solves. The median per-input ratio is 0.970: about 3% faster, effectively similar in this single-run sample. Including compilation, the median ratio of the slowest new compile-plus-solve duration to the old compile-plus-solve duration is 0.984, about 2% faster. The baseline improves from 2.253 to 1.368 seconds native solving (about 39% faster), or 2.489 to 1.568 seconds including compilation. Other inputs regress, so the baseline improvement is not representative of every case.

These are child solver durations, not measured end-to-end latency with persistent warm workers. Taking their maximum assumes overlapping work and excludes differing task start offsets, model construction, extraction and transport. The four production timeouts have no accepted complete results and are excluded. The two completed finer-grid pairs are too small and survivor-biased to establish performance across that grid. Three processes perform three solves; total CPU time and memory consumption were not measured. The current implementation still recreates those processes on each call, so treating startup as a one-time application cost requires a persistent worker pool and a separate warm-pool benchmark.

| Input | Production previous s | Production three workers s | Finer previous s | Finer three workers s |
| --- | ---: | ---: | ---: | ---: |
| baseline | 2.725 | 4.089 | N/A, timeout | N/A, timeout |
| battery_4 | 2.551 | N/A, timeout | N/A, timeout | N/A, timeout |
| battery_7.9 | 2.624 | 4.235 | 4.844 | N/A, timeout |
| battery_8.1 | 2.020 | 4.992 | N/A, timeout | N/A, timeout |
| battery_14 | 1.679 | 3.799 | 1.580 | 4.438 |
| solar_0.75 | 3.637 | N/A, timeout | 3.647 | N/A, timeout |
| solar_0.95 | 2.325 | N/A, timeout | 3.232 | N/A, timeout |
| solar_1.05 | 2.034 | 4.341 | 4.448 | N/A, timeout |
| solar_1.25 | 1.980 | 3.947 | 3.383 | N/A, timeout |
| load_0.75 | 2.177 | 4.505 | 2.378 | N/A, timeout |
| load_0.95 | 1.756 | 4.613 | 3.163 | N/A, timeout |
| load_1.05 | 2.506 | 4.669 | 2.444 | N/A, timeout |
| load_1.25 | 1.487 | 4.761 | 3.872 | N/A, timeout |
| preference_0 | 0.779 | 3.347 | 0.836 | 3.801 |
| preference_50 | 1.983 | 4.461 | 3.276 | N/A, timeout |
| reserve_2 | 1.503 | 4.702 | 4.746 | N/A, timeout |
| reserve_6 | 1.641 | 4.175 | 1.721 | N/A, timeout |
| tariff_flat | 2.650 | 4.862 | 4.763 | N/A, timeout |
| tariff_peak_1.5 | 2.238 | 4.513 | 4.136 | N/A, timeout |
| solar_4 | 1.512 | 4.950 | Not tested | Not tested |
| load_4 | 2.624 | N/A, timeout | Not tested | Not tested |

## Evidence

- Previous port-model SHA-256: `d9d5e588db87478dd8035c0a3338279a843f5f5a97c36e008add737772763595`.
- Fixed-mode model SHA-256: `1a9d07520cd790a7339a24375de637a3d1420a417600f92465a5e0206b699544`.
- Unchanged configuration SHA-256: `c57bb64435b71a3a3f0c70c7a8c88e176c62ebff41c51282d72472f303ec990e`.

Local ignored evidence under `documentation/reviews/system-review-evidence/port-model-comparison/` contains the old/new source snapshots, JSON results (`fixed-mode-calibration.json`, `fixed-mode-production.json`, `fixed-mode-historical.json`) and process logs. Prior results remain in `initial.json` and `production-extra.json`. These are single-run measurements for the stated snapshots, not production latency guarantees or annual savings estimates.
