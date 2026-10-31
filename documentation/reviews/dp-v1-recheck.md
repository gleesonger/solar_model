# DP v1 verification, 2026-10-30

The restored v1 planner evaluates all nine standard scenarios and selects one shared first command. Fresh configured-grid comparisons completed in 0.571-0.614 seconds for DP versus 2.974-5.931 seconds for MILP, including validation. DP's combined score was 1.189-1.822% lower, with EUR 0.061-0.102 higher expected net cost over 48 elapsed hours. These are single measurements on synthetic forecasts, not installation savings or latency guarantees.

The solver and its default 5-percentage-point SOC value grid were not changed for this initial check. The current implementation is [dynamic_programming.py](../../solar_model/planning/plan_optimisation_dynamic_programming.py); the [previous investigation](planning-dynamic-programming-comparison.md) records older measurements separately. The later [tariff-hoisting comparison](#tariff-hoisting-runtime-comparison) below measures the subsequent input-preparation optimisation separately from the initial results.

## Nine scenarios and shared first action

Standard inputs come from `create_scenarios`, combining three solar cases and three load cases. Their normalised probabilities are `0.04, 0.12, 0.04, 0.12, 0.36, 0.12, 0.04, 0.12, 0.04` in scenario order. The numerical solver also accepts other scenario-list sizes; nine is supplied by the standard scenario builder rather than imposed as a hard-coded solver count.

The source implements the following sequence:

1. Build a complete backward future-value table for each supplied scenario.
2. Try each of the seven first commands: passive self-consumption, or charge/discharge at one-third, two-thirds or full hardware power.
3. For each candidate, start every scenario from the same measured energy and pass the identical first `(mode, power)` into its rollout. The first interval's global schedule bounds supply the same command SOC stops.
4. Keep that first command fixed during the first interval. Select later actions separately for each scenario, carrying actual energy forwards without SOC rounding.
5. Sum each scenario's full-horizon cost/protection/terminal score using its probability. Accept a first command only if every scenario has a feasible rollout. A candidate can stop being checked early when one required scenario fails.
6. Return one `NextAction` and nine scenario result frames for the winning command.

Every completed benchmark returned nine named scenario results. In each configured-grid DP case, all nine first-period flow tuples were distinct even though the first command was shared. Different realised energy/import/export amounts reflect different PV/load forecasts, rather than separate first controls.

The new targeted test `test_forced_first_command_is_shared_and_selected_using_all_nine_scenarios` audits calls during a real solve. Cheap first-period imports cause forced charging, while a restricted AC absorption rating and varying PV make achieved charge differ by scenario. The test verifies:

- Nine distinct scenario inputs receive the selected identical command, including its SOC stops.
- The chosen command's realised weighted score equals the best complete candidate score across all seven tested policies.
- The returned first rows match those scenario rollouts.
- Pinning the same returned command in MILP reproduces first-period end energy, grid import/export and solar clipping for all nine scenarios within 0.000001 kWh.

The existing passive-first nine-scenario MILP replay test also passed. This checks declared software response, not installed-firmware behaviour.

## Accuracy and performance on the configured MILP grid

Both models use the existing configured mixed time grid, not a full day of quarter-hours: 28 visible intervals from the 10:05 UTC synthetic start, followed by six hidden four-hour intervals. The same forecasts, probabilities, tariffs, equipment ratings, efficiencies, hard limits, protection approximation and terminal valuation are supplied to each model. MILP remains the application default; the benchmark's `lp` label selects the mixed-integer CVXPY/HiGHS solver.

The device is the benchmark's synthetic 18 kWh / 5 kW system with 95% charge/discharge efficiencies. The base initial energy is 8 kWh; the `battery_4` case changes it to 4 kWh. Costs and scores below cover all 48 elapsed hours and use normalised probabilities. Negative cost means revenue. The score gap is `(MILP score - DP score) / abs(MILP score)`.

| Case | DP seconds | MILP seconds | Speedup | DP score | MILP score | Score gap | DP cost EUR | MILP cost EUR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 0.586 | 2.974 | 5.08x | 5.357562 | 5.436361 | 1.449% | 0.367433 | 0.275535 |
| Initial battery 4 kWh | 0.578 | 3.319 | 5.74x | 4.581301 | 4.636418 | 1.189% | 1.117199 | 1.056450 |
| Solar x1.25 | 0.614 | 5.760 | 9.38x | 6.244982 | 6.338458 | 1.475% | -0.522242 | -0.624241 |
| Load x1.25 | 0.595 | 5.931 | 9.96x | 4.084471 | 4.160266 | 1.822% | 1.633635 | 1.543688 |
| Protection preference 0 | 0.571 | 3.819 | 6.69x | 4.645598 | 4.730098 | 1.786% | 0.351734 | 0.267234 |

DP calculation time before the independent validation was 0.502-0.546 seconds in those cases. The largest individual-scenario full-cost increase was approximately EUR 0.142. These results establish closeness on the selected cases, not a guaranteed objective-error bound.

DP selected self-consumption in all five cases. MILP also selected it for the 4 kWh case, but used small forced rates or already-reached stops in other cases. V1's seven powers and fixed schedule-bound stops exclude some of those choices. First commands therefore need not match numerically even when total scores are close.

## Finer DP timing, retaining the configured MILP grid

The separate full-quarter-hour DP baseline uses 96 visible intervals and six hidden four-hour intervals. It completed in 2.730 seconds including validation, with a full-horizon score of 5.361041 and net cost EUR 0.370263.

Compared with the configured-grid MILP baseline above, its score is 1.385% lower and its runtime is close to MILP's 2.974 seconds. This is an operational comparison using different time discretisations, not an optimality gap between identical finite models. Coarse/fine intervals also differ in aggregation and interval-start tariff approximations.

A full-quarter-hour MILP attempt hit its 120-second controller deadline and was terminated/reaped as a process tree. Its result is N/A; no cost or accuracy is inferred from that timeout. The user subsequently clarified that MILP should retain its configured, less granular grid, so no further full-quarter-hour MILP runs were made. Accuracy conclusions in the main table use the five completed configured-grid MILP references.

## Verification boundaries and reproduction

Seven targeted tests passed using:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/planning/test_dynamic_programming.py
```

No full suite was run. Tests include seven-action construction, interpolation without physical rounding, command SOC stops, unsafe export/passive-response rejection, exhaustive agreement on a small grid-aligned example, passive-first MILP replay and the new forced shared-command audit/replay.

Every completed benchmark validates duration, visible solar/load/battery conservation, billing, physical power, energy/export bounds, direction exclusions and visible/hidden score components. MILP report scores match the native objective within 0.000001. V1 reports a realised feasible policy score, not an optimality certificate.

Fresh processes run sequentially with BLAS/OpenMP limits of one. Imports and input preparation are outside the timed ready/start handshake; reported totals include simulation and validation. The previously authorised 120-second limit was used explicitly for each model. No database or hardware connection was accessed. Environment: Windows, repository Python 3.12.10 virtual environment. Do not compare these single timings as if they were repeated statistical measurements or directly interchangeable with the older Python 3.11 measurements.

```powershell
.\.venv\Scripts\python.exe -m solar_model.tools.benchmark_planning --model dp --grid production --cases baseline battery_4 solar_1.25 load_1.25 preference_0 --cap-seconds 120 --output dp-v1-production.json
.\.venv\Scripts\python.exe -m solar_model.tools.benchmark_planning --model lp --grid production --cases baseline battery_4 solar_1.25 load_1.25 preference_0 --cap-seconds 120 --output milp-production.json
.\.venv\Scripts\python.exe -m solar_model.tools.benchmark_planning --model dp --grid quarter-hour --cases baseline --cap-seconds 120 --output dp-v1-quarter-hour.json
```

Raw records, including the terminated MILP attempt, are retained locally under ignored `documentation/reviews/system-review-evidence/dp-v1-recheck/`. They contain first-step scenario flows, costs, score components, source/configuration hashes and timings. The restored DP source hash is `7340dd6208b72b40f4093381cb7f8ea98f9d8f31a5214f828893191f06c13b9b`; the unchanged MILP/scoring hash is `753d68209d8ab6d3a2915f3bf5f159502f9a0eb8ce3564874be31cf7b21a57d7`.

The existing [DP approximation finding](findings.md) still applies: restricted commands, fixed stops and interpolated value tables can miss better feasible policies. Forecast uncertainty, installed-device response and SOC conventions remain separate limitations.

## Tariff-hoisting runtime comparison

The subsequent optimisation resolves tariffs, schedules, duration and protection weight once per interval, reusing them across scenarios. Before/after tests used the same nine-scenario synthetic baseline on the configured grid and, separately, the full-quarter-hour DP grid. No MILP runs were required for this comparison.

Each variant ran three times in fresh processes, alternating before/after order, sequentially. Imports and input preparation in the benchmark worker preceded its timed ready/start handshake. Within the timed public simulation, an additional timer measured from function entry to the first backward-value-table call, covering input validation, score constants, all scenario periods, the SOC grid and action preparation. The same lightweight timing wrappers were used for both variants. The authorised 120-second per-model deadline was enforced; no run timed out.

The baseline was loaded from the restored v1 stash source into an isolated worker's module namespace, rather than swapping the working file. Its normalised source before and after the changed preparation block was verified identical to the current version. The updated working file stayed in place throughout and was checked byte-for-byte afterwards.

| Grid / measurement | Before median | After median | Median change |
| --- | ---: | ---: | ---: |
| Configured: input preparation | 7.327 ms | 3.212 ms | 56.2% less |
| Configured: public simulation | 305.998 ms | 288.555 ms | 5.7% less |
| Configured: simulation plus validation | 342.090 ms | 325.888 ms | 4.7% less |
| Quarter-hour DP: input preparation | 17.980 ms | 8.339 ms | 53.6% less |
| Quarter-hour DP: public simulation | 874.782 ms | 904.303 ms | 3.4% more |
| Quarter-hour DP: simulation plus validation | 917.247 ms | 944.256 ms | 2.9% more |

Input preparation consistently became faster: approximately 4 ms saved on the configured grid and 10 ms on the quarter-hour grid. A consistent overall speedup was not established. Public simulation ranges overlap: configured before 282.517-309.880 ms versus after 287.534-338.441 ms; quarter-hour before 849.957-898.805 ms versus after 834.154-911.606 ms. With only three observations per variant, the total changes should not be attributed entirely to lookup hoisting or treated as statistical latency bounds. Most time remains in DP value calculation and policy rollouts.

All 12 runs passed the benchmark's physical/report validation. Selected first commands, all nine scenario-result metric dictionaries (including first-step flows, costs and score components) and weighted full scores matched exactly between variants and repetitions on each grid. Solver resolution and command choices were unchanged.

The tested hoisted solver SHA-256 was `e4ef31777f36d241b5a466558ca28cab4d9c41db496de7503f8da18784e2ac62`; the earlier source's LF hash is `cced0a695a4a223c0dbadb632ece1c20e4710f1de31462afe8de3903849c7846`, equivalent to the initial recheck's CRLF hash above. Raw records, the exact earlier source and the runnable comparison harness are retained locally under ignored `documentation/reviews/system-review-evidence/dp-tariff-hoisting/`. The comparison harness requires the hoisted solver version to reproduce these measurements; its original invocation from the repository root was `.\.venv\Scripts\python.exe documentation/reviews/system-review-evidence/dp-tariff-hoisting/compare.py` while those local artifacts are available. This is a fresh same-session comparison; the older headline runtimes above were not used as its timing baseline.

The tariff-hoisting change was subsequently reverted at the user's request. Current period assembly is scenario-first again; the measurements above remain historical evidence for the experiment.
