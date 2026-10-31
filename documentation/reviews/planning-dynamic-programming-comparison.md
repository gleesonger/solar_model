# Dynamic-programming comparison, 2026-10-29

This is the earlier investigation. See the [2026-10-30 v1 recheck](dp-v1-recheck.md) for fresh measurements of the restored implementation and an explicit nine-scenario forced first-command audit. Historical measurements below retain their original environment and deadline.

The experimental DP planner completed all eight synthetic cases within 1.66 seconds including validation. On four completed configured-grid pairs, it was 1.62-2.88 times faster than the current mixed-integer solver, with expected 48-hour net costs EUR 0.060-0.101 higher and combined scores 1.20-1.83% lower. These are single observations, not production latency or savings guarantees. LP remains the application default.

The requested quarter-hour experiment used 96 visible decisions, plus the same six hidden four-hour slots in both models. DP completed all three cases; LP exceeded its five-second deadline in each, so no quarter-hour LP economic comparison is available. No timed-out case was retried.

## Compared models and scope

- DP: 5-percentage-point SOC value grid, augmented by exact initial energy and schedule boundaries; seven actions (self-consumption and L/M/H charge/discharge). L/M/H are one-third, two-thirds and full hardware power. SOC stops are fixed at configured energy bounds.
- LP: current CVXPY/HiGHS mixed-integer model, including the command/response limit correction; continuous power/SOC choices and independently optimised later port flows.
- Both: identical nine scenarios, weights, forecasts, tariffs, efficiencies, user/hardware limits, visible intervals, 24-hour hidden extension, chord protection utility, protection preference, terminal credit and initial state for each paired case. Later decisions can adapt separately by scenario, as in the current two-stage information assumption.
- DP does not round physical energy. It interpolates future value, then re-evaluates the policy at actual states. Seven shared-first-action rollouts are scored from actual feasible outcomes. A 5-point grid does not bound economic error to 5%, and interpolation is not guaranteed to find the discrete optimum.
- Model differences include the action set and later mode restrictions, not just interpolation. In particular, LP can choose a tiny power or an intermediate/already-reached SOC stop to hold battery energy; DP does not search those additional commands. The observed gap cannot be attributed solely to grid resolution.

## Configured-grid results

28 visible intervals (the configured 15-minute/hourly mixture starting at 10:05 UTC), then six hidden slots. All costs/scores below cover the full 48 elapsed hours and are probability-weighted across nine scenarios. Negative net cost is revenue. Timings include physical/report validation and exclude process/import/input-preparation startup.

| Case | DP seconds | LP seconds | DP net cost EUR | LP net cost EUR | DP score | LP score | Score gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 0.613 | N/A | 0.367 | N/A | 5.358 | N/A | N/A |
| battery_4 | 1.237 | 3.346 | 1.117 | 1.057 | 4.581 | 4.637 | 1.20% |
| solar_1.25 | 1.129 | 2.894 | -0.522 | -0.623 | 6.245 | 6.339 | 1.48% |
| load_1.25 | 1.150 | 3.306 | 1.634 | 1.544 | 4.084 | 4.160 | 1.83% |
| preference_0 | 1.195 | 1.940 | 0.352 | 0.267 | 4.646 | 4.730 | 1.79% |

The configured-grid baseline LP timed out after five seconds. The other four pairs passed all validations. Maximum individual-scenario full-cost increase was EUR 0.141487; the weighted increases were EUR 0.059864, 0.101164, 0.089683 and 0.084500 respectively. No annual extrapolation was made.

## Quarter-hour results

| Case | DP calculation seconds | DP seconds incl. validation | LP seconds | DP full net cost EUR | DP full score |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 1.588 | 1.659 | N/A (>5 s) | 0.370 | 5.361 |
| solar_1.25 | 1.551 | 1.620 | N/A (>5 s) | -0.516 | 6.249 |
| preference_0 | 1.523 | 1.598 | N/A (>5 s) | 0.357 | 4.640 |

DP had a 300-second deadline, as requested. No DP run approached it. LP retained the repository five-second deadline. Thus quarter-hour timings establish completed DP runtimes and LP deadline failures, not a measured LP speed ratio or score gap.

## Commands and per-scenario outcomes

DP selected self-consumption in all eight cases. This is a result of these inputs, not a hard-coded restriction. On completed configured-grid LP cases, battery_4 and load_1.25 also selected self-consumption. solar_1.25 selected charge at 0.1 kW to a 100% SOC stop. preference_0 selected charge at 5 kW with a 0% SOC stop, yielding zero forced charge from the initial state. Inactive SOC fields are ignored. These are not identical commands even when scores are close.

The following full-horizon outcomes use the same scenario names and inputs within each pair. Values are EUR for net cost/score and kWh for final battery energy.

### battery_4

| Scenario | DP cost | LP cost | DP score | LP score | DP final kWh | LP final kWh |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low_low | 0.346 | 0.312 | 5.368 | 5.397 | 18.000 | 18.000 |
| low_med | -1.373 | -1.470 | 7.108 | 7.185 | 18.000 | 18.000 |
| low_high | -3.156 | -3.255 | 8.885 | 8.970 | 18.000 | 18.000 |
| med_low | 2.917 | 2.886 | 2.725 | 2.762 | 18.000 | 18.000 |
| med | 1.022 | 0.982 | 4.688 | 4.730 | 18.000 | 18.000 |
| med_high | -0.706 | -0.802 | 6.436 | 6.515 | 18.000 | 18.000 |
| high_low | 6.303 | 6.284 | -0.697 | -0.663 | 18.000 | 18.000 |
| high_med | 3.659 | 3.585 | 1.997 | 2.069 | 18.000 | 18.000 |
| high_high | 1.747 | 1.656 | 3.983 | 4.053 | 18.000 | 18.000 |

### solar_1.25

| Scenario | DP cost | LP cost | DP score | LP score | DP final kWh | LP final kWh |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low_low | -0.744 | -0.816 | 6.477 | 6.551 | 18.000 | 18.000 |
| low_med | -2.953 | -3.050 | 8.685 | 8.785 | 18.000 | 18.000 |
| low_high | -5.168 | -5.269 | 10.901 | 11.004 | 18.000 | 18.000 |
| med_low | 1.644 | 1.601 | 4.067 | 4.113 | 18.000 | 18.000 |
| med | -0.527 | -0.635 | 6.241 | 6.349 | 18.000 | 18.000 |
| med_high | -2.722 | -2.864 | 8.462 | 8.579 | 18.000 | 18.000 |
| high_low | 4.122 | 4.100 | 1.542 | 1.562 | 18.000 | 18.000 |
| high_med | 1.950 | 1.819 | 3.792 | 3.887 | 18.000 | 18.000 |
| high_high | -0.279 | -0.408 | 6.016 | 6.119 | 18.000 | 18.000 |

### load_1.25

| Scenario | DP cost | LP cost | DP score | LP score | DP final kWh | LP final kWh |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low_low | 0.275 | 0.232 | 5.445 | 5.500 | 18.000 | 18.000 |
| low_med | -1.449 | -1.553 | 7.188 | 7.285 | 18.000 | 18.000 |
| low_high | -3.230 | -3.338 | 8.962 | 9.070 | 18.000 | 18.000 |
| med_low | 3.293 | 3.268 | 2.397 | 2.421 | 18.000 | 18.000 |
| med | 1.609 | 1.488 | 4.135 | 4.225 | 18.000 | 18.000 |
| med_high | -0.191 | -0.295 | 5.908 | 6.010 | 18.000 | 18.000 |
| high_low | 7.438 | 7.403 | -1.808 | -1.755 | 18.000 | 18.000 |
| high_med | 4.675 | 4.624 | 0.993 | 1.033 | 18.000 | 18.000 |
| high_high | 2.891 | 2.778 | 2.842 | 2.926 | 18.000 | 18.000 |

### preference_0

| Scenario | DP cost | LP cost | DP score | LP score | DP final kWh | LP final kWh |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low_low | -0.334 | -0.370 | 5.331 | 5.367 | 18.000 | 18.000 |
| low_med | -2.082 | -2.157 | 7.079 | 7.154 | 18.000 | 18.000 |
| low_high | -3.859 | -3.941 | 8.856 | 8.938 | 18.000 | 18.000 |
| med_low | 2.071 | 2.031 | 2.927 | 2.967 | 18.000 | 18.000 |
| med | 0.335 | 0.244 | 4.662 | 4.754 | 18.000 | 18.000 |
| med_high | -1.417 | -1.540 | 6.415 | 6.538 | 18.000 | 18.000 |
| high_low | 4.765 | 4.746 | 0.232 | 0.251 | 18.000 | 18.000 |
| high_med | 2.814 | 2.709 | 2.184 | 2.288 | 18.000 | 18.000 |
| high_high | 1.051 | 0.924 | 3.947 | 4.074 | 18.000 | 18.000 |

## Validation and reproduction

Six targeted tests in `tests/planning/test_dynamic_programming.py` passed. They cover seven-action construction, value interpolation, command-encoded SOC limits, unsafe export/passive-response rejection, exhaustive-search agreement on a small grid-aligned case and first-command replay through LP across nine scenarios. No full suite was run.

Every completed benchmark independently validated duration, solar/load/battery conservation, billing, battery/inverter power, energy bounds, battery-export caps, mutually exclusive grid/battery directions and visible/hidden score reconstruction. LP report scores additionally matched its native solver objective within 0.000001. DP has a realised policy score, not an optimality certificate. The visible-period metadata was corrected to the validated visible timestep count; measured timings and economic results were unchanged.

Run from the repository root, with no concurrent tests/benchmarks:

```powershell
python -m solar_model.tools.benchmark_planning --model dp --grid production --cases baseline battery_4 solar_1.25 load_1.25 preference_0 --cap-seconds 300 --output dp-production.json
python -m solar_model.tools.benchmark_planning --model lp --grid production --cases baseline battery_4 solar_1.25 load_1.25 preference_0 --output lp-production.json
python -m solar_model.tools.benchmark_planning --model dp --grid quarter-hour --cases baseline solar_1.25 preference_0 --cap-seconds 300 --output dp-quarter-hour.json
python -m solar_model.tools.benchmark_planning --model lp --grid quarter-hour --cases baseline solar_1.25 preference_0 --output lp-quarter-hour.json
```

Fresh worker preparation/imports are outside the timed handshake. Each case is run once, sequentially, with BLAS/OpenMP limits set to one. The controller kills/reaps the process tree on timeout, records N/A and moves to the next case. Raw JSON is retained locally under ignored `documentation/reviews/system-review-evidence/dynamic-programming/`.

Environment: Windows-10-10.0.26200-SP0; Python 3.11.8; 20 logical CPUs. Case construction and synthetic device ratings are in `solar_model/tools/benchmark_planning.py`. No user database or device was accessed.

Source/configuration SHA-256:

- DP: `cced0a695a4a223c0dbadb632ece1c20e4710f1de31462afe8de3903849c7846`
- LP and scoring helpers: `753d68209d8ab6d3a2915f3bf5f159502f9a0eb8ce3564874be31cf7b21a57d7`
- Physical/report helper: `8226c30ebf5ca2ade7bd5aa8e98fdee3867376ec74b95e8b6611de429f7dd5b8`
- Configuration: `c57bb64435b71a3a3f0c70c7a8c88e176c62ebff41c51282d72472f303ec990e`

The production entry point remains unchanged. Before adopting DP as the default, the discretisation and fixed-stop/action restrictions need evaluation over representative real forecast cutoffs and restrictive schedules. Installed-firmware response and existing SOC-convention questions remain unverified.
