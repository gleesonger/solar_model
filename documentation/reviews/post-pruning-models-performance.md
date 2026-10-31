# MILP versus DP after command pruning

Measured 2026-10-31 on the local Windows laptop with Python 3.12.10, after applying lookup caching and command pruning to production DP. The laptop was connected to AC power when checked during the test (74% battery). This is a synthetic comparison, not measured installation savings.

## Procedure

Both models used the same synthetic baseline as the [earlier comparison](current-models-performance.md): nine weighted solar/load scenarios, an 18 kWh battery initially holding 8 kWh, 95% efficiencies, protection preference 50 and preferred reserve 3 kWh. The projection starts at 2026-10-27 10:05 UTC, with 24 reported hours and 24 hidden lookahead hours. No database or device was accessed.

Current configured intervals were retained: MILP uses 28 visible periods (`[15*4, 60*23]`), DP uses 96 quarter-hour periods, and both append six four-hour lookahead periods. DP uses Numba and stages `(2% /5 unrestricted) -> (1% /10 +/-5%) -> (1% /20 +/-3%) -> (1% /100 +/-1%)`.

Four sequential calls per model alternated MILP then DP, with one reusable worker per model. The first call of each was discarded from timing averages, leaving three measured calls each. This excludes initial import/cache-loading/compilation overhead from the means without adding a synthetic warm-up. Numba's existing disk cache was retained; these first-call times do not measure guaranteed cold compilation latency. MILP builds a fresh problem on each call, with warm starts disabled.

Simulation time includes planner preparation, all stages and report construction. Input generation and worker startup are excluded; total validated time additionally includes the benchmark's independent checks. Each run had a 120-second deadline. All eight completed, passed validation and retained unchanged source hashes.

## Results

Scores, costs, battery endpoints and all nine scenario summaries were identical across the four calls within each model. Battery and cost results below are probability-weighted.

| Measurement | MILP | DP |
| --- | ---: | ---: |
| Mean simulation time, first call excluded | 2.826 s | 1.848 s |
| Retained simulation range | 2.521-3.347 s | 1.778-1.970 s |
| Mean including independent validation | 2.902 s | 1.912 s |
| Full 48-hour score | 5.436361299 | 5.412220603 |
| Visible 24-hour net cost | EUR 0.477029472 | EUR 0.436576375 |
| Full 48-hour net cost | EUR 0.275535185 | EUR 0.311107398 |
| Battery at 24 hours | 15.262703248 kWh | 14.882613759 kWh |
| Battery at 48 hours | 18 kWh | 18 kWh |

DP took 34.6% less simulation time than MILP in this batch (1.53 times the throughput). MILP's range is wider, so these three retained calls do not establish a general runtime bound. Absolute timings should not be directly compared with the earlier experiment's different machine conditions to measure the pruning gain; the [paired optimisation experiment](dp-loop-optimisation-experiment.md) addresses that separately.

DP's full score is 0.444% lower and its full-horizon net cost is EUR 0.035572213 higher. Both models reproduce their earlier economics. Their timestep grids and protection-score approximations differ, so native score differences are not an isolated measure of optimisation accuracy. Commands have not been replayed through a common independent simulator.

MILP selects forced charge at 0.1 kW with a 44.444% charge stop, already the initial SOC. DP selects self-consumption. Both models retain a shared first command across all scenarios, with scenario-specific later decisions.

| Call | MILP simulation | DP simulation | Included in means |
| --- | ---: | ---: | --- |
| 1 | 3.553 s | 4.267 s | No |
| 2 | 3.347 s | 1.778 s | Yes |
| 3 | 2.610 s | 1.970 s | Yes |
| 4 | 2.521 s | 1.795 s | Yes |

## DP timing breakdown

Means sum per-pass counters across all stages. Counters include surrounding Python/container overhead as well as compiled work. Percentages use mean simulation time; rollout timing excludes separately recorded realised scoring.

| Step | Mean | Share |
| --- | ---: | ---: |
| Backward value tables | 0.5805 s | 31.4% |
| Forward rollouts and command selection | 0.4953 s | 26.8% |
| Grids, commands and typed-input preparation | 0.3841 s | 20.8% |
| Report construction | 0.2280 s | 12.3% |
| Validation, scoring setup and forecast/tariff preparation | 0.1427 s | 7.7% |
| Realised path scoring | 0.0071 s | 0.4% |
| Refinement orchestration and other overhead | 0.0102 s | 0.6% |

| Stage | Mean time | Retained score |
| --- | ---: | ---: |
| 2% /5 unrestricted | 0.7061 s | 5.383829473 |
| 1% /10 +/-5% | 0.3467 s | 5.398027273 |
| 1% /20 +/-3% | 0.3461 s | 5.407699117 |
| 1% /100 +/-1% | 0.4461 s | 5.412220603 |

Every refinement was accepted in all calls.

## Evidence

The benchmark independently checks solar/load/battery balances, hardware/SOC/export constraints, exclusivity, durations and cost arithmetic across visible and hidden results. MILP report scores also match its native objective within the benchmark tolerance. All checks passed.

Ignored local evidence under `documentation/reviews/system-review-evidence/` comprises `compare_post_pruning_models.py`, `post-pruning-models-config.yaml` and `post-pruning-models-repeated.json`. The JSON includes every run, validation outcomes, source/config hashes, DP phase/stage counters and first-call-excluded summaries. Run the driver from the repository root with `.venv\Scripts\python.exe documentation/reviews/system-review-evidence/compare_post_pruning_models.py`; it snapshots the then-current configuration and enforces the deadline. Earlier evidence files were preserved.

Configuration SHA-256: `3ab1a393c3c1e0703762ec56f53a134c0f29751db1f68a5feda862c2be336f6b`.
