# Current-config MILP and DP comparison

Measured 2026-10-31 on the local Windows 11 laptop using Python 3.12.10. This is a synthetic baseline comparison using the current configuration, not measured installation savings. No optimiser code or configuration was changed for the experiment.

## Inputs and procedure

Both models received the same nine weighted solar/load scenarios, tariffs, 18 kWh battery, 8 kWh initial energy and 95% charge/discharge efficiencies. The synthetic projection starts at 2026-10-27 10:05 UTC and covers 24 reported hours plus 24 hours of lookahead. Protection preference is 50 and preferred reserve is 3 kWh.

Each model retained its own configured intervals: MILP has 28 reported periods from `[15*4, 60*23]`; DP has 96 quarter-hour periods. Both append six four-hour lookahead periods. DP uses Numba and these stages:

```text
(2% /5, full range)
-> (1% /10, +/-5 percentage points)
-> (1% /20, +/-3 percentage points)
-> (1% /100, +/-1 percentage point)
```

There were four calls per model, alternating MILP then DP, with a reusable worker process for each model. The first call of each was discarded from every timing average. The remaining three calls reuse imported libraries and Numba dispatchers; MILP still builds a fresh optimisation problem with warm starts disabled. No synthetic warm-up was added and the Numba disk cache was not cleared. The discarded DP call includes first-use import/cache loading/compilation overhead, so it is not a measurement of guaranteed cold compilation latency.

Timers cover model preparation, calculation and report extraction. Synthetic input construction, imports before the worker handshake and process startup are excluded. `total_seconds` additionally includes independent physical/report validation. Each call had a 120-second deadline. All eight completed and passed validation; source hashes were unchanged throughout the test. The configuration was snapshotted before starting the workers.

## Results

All four calls of each model had exactly matching per-scenario results, score components, costs, battery endpoints, first-step reported flows and selected command. Results below are probability-weighted across scenarios.

| Measurement | MILP | DP |
| --- | ---: | ---: |
| Mean simulation time, first call excluded | 1.619 s | 1.627 s |
| Retained simulation range | 1.591–1.645 s | 1.577–1.665 s |
| Mean including output validation | 1.656 s | 1.668 s |
| Full 48-hour score | 5.436361299 | 5.412220603 |
| Reported 24-hour net cost | EUR 0.477029472 | EUR 0.436576375 |
| Full 48-hour net cost | EUR 0.275535185 | EUR 0.311107398 |
| Battery at 24 hours | 15.262703248 kWh | 14.882613759 kWh |
| Battery at 48 hours | 18 kWh | 18 kWh |

Runtime is effectively tied in this sample: DP's mean is 0.009 seconds higher (0.54%), within the observed run-to-run spread. DP's native score is 0.444% lower, and its full-horizon net cost is EUR 0.035572213 higher. The models have different timestep grids and protection approximations, so the score difference is not a controlled measure of search accuracy.

MILP selected forced charge at 0.1 kW with a 44.444% charge stop, which is already the initial SOC. DP selected self-consumption. These are native model outputs, not commands replayed through an independent common simulator.

| Call | MILP simulation | DP simulation | Included in means |
| --- | ---: | ---: | --- |
| 1 | 1.777 s | 5.213 s | No |
| 2 | 1.619 s | 1.640 s | Yes |
| 3 | 1.591 s | 1.665 s | Yes |
| 4 | 1.645 s | 1.577 s | Yes |

## Where DP spends its time

Means below sum the existing per-pass timing counters across all four stages for each retained run. Percentages use the mean whole-simulation time. These counters include Python/container overhead around each step as well as compiled work; they do not isolate CPU instructions inside Numba.

| Step | Mean | Share |
| --- | ---: | ---: |
| Forward rollouts and command selection | 0.7622 s | 46.84% |
| Backward value tables | 0.4435 s | 27.25% |
| Grids, commands and typed-input preparation | 0.2051 s | 12.61% |
| Report construction | 0.1284 s | 7.89% |
| Validation, scoring setup and forecast/tariff preparation | 0.0783 s | 4.81% |
| Realised path scoring | 0.0036 s | 0.22% |
| Refinement orchestration and other overhead | 0.0063 s | 0.38% |

| DP stage | Mean stage time | Retained score after stage |
| --- | ---: | ---: |
| 2% /5, full range | 0.3449 s | 5.383829473 |
| 1% /10, +/-5 points | 0.2221 s | 5.398027273 |
| 1% /20, +/-3 points | 0.2868 s | 5.407699117 |
| 1% /100, +/-1 point | 0.7720 s | 5.412220603 |

Every refinement was accepted in every call. The final /100 stage takes about 47% of the total runtime. Forward and backward calculations together account for about 74% of DP runtime.

## Evidence and validation

The existing benchmark worker checks solar, load and battery balances; battery/inverter/export limits; exclusive charging/discharging and grid import/export; interval durations; and cost arithmetic across reported and hidden periods. For MILP, the reconstructed weighted report score also matches the native solver objective within 0.000001. All checks passed in all eight calls.

Local generated evidence is under ignored `documentation/reviews/system-review-evidence/`:

- `compare_current_models.py`: reusable-worker driver calling the existing benchmark worker four times per model and recording its DP timing output.
- `current-models-config.yaml`: exact configuration snapshot.
- `current-models-repeated.json`: all eight records, stdout phase counters, validation outcomes, hashes and means with run index 0 excluded.

Run the retained driver from the repository root with `python documentation/reviews/system-review-evidence/compare_current_models.py`. It snapshots the then-current configuration and enforces the 120-second deadline. The ordinary benchmark CLI creates a fresh worker for each case, so invoking it repeatedly does not reproduce this reusable-worker timing scope.

Configuration SHA-256: `3ab1a393c3c1e0703762ec56f53a134c0f29751db1f68a5feda862c2be336f6b`.
