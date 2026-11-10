# Markov dynamic programming

The shipped configuration selects `energy_plan.optimiser.dynamic_programming.uncertainty_model: markov`. The older calculation remains available as `independent_scenarios`. The uncertainty mode, transition duration and matrix must all be explicitly specified; missing keys fail configuration validation.

The original [proposal](../reviews/markov-dp-proposal.md) records the reasoning and agreed matrix. This guide describes the implemented contracts. General physics and score definitions remain in the [DP walkthrough](dynamic-programming.md) and [plan model](../overview/plan-model.md).

## Ownership and inputs

`plan_optimisation_dynamic_programming.simulate_energy_plan()` retains public validation, time-grid preparation, scoring scales and progress/failure logs. In Markov mode it runs only the first full-range configured refinement stage. Explicit `grid_step_percent` and `power_divisions` still override that stage. `battery_bounds` is rejected: old per-path corridors do not describe reachable states when conditions can change.

After assembling and validating scenario-period inputs, `solve_pass()` calls `planning.markov.solve_prepared()`. That module owns duration-scaled transitions, coupled backward values, forward probability propagation and Markov result assembly. Both modes reuse the existing command simulator, physical limits, route allocation and exact nonlinear protection formula.

Nine uniquely named input scenarios are required: `med` for medium/medium and `<load>_<solar>` for all other low/med/high combinations. Internal layer order is explicitly load-major then solar-major; input list order does not matter. Medium multipliers must be positive.

Markov period inputs are reconstructed from the medium scenario and configured multipliers. The old high-case half-hour timing redistribution is not used. No additional solar correction or actuals-based load scaling is performed: the service already supplies the as-of intraday-adjusted solar projection and existing load baseline.

## States, uncertainty and information

A value-table node is `(time boundary, battery kWh, current load/solar condition)`. All layers share one full-range battery grid, augmented with exact current energy and clipped schedule boundaries. The initial state is the actual battery energy and medium/medium condition with probability one.

For every candidate command at a node:

1. Choose the command before the interval outcome is known.
2. Simulate that command under each destination condition's interval solar/load.
3. Add period score to interpolated continuation value in that destination layer.
4. Weight outcomes by the source condition's transition probabilities.
5. Compare these expected command scores and retain the highest.

The next decision may depend on the destination condition only after the interval has completed. Exact ties retain the first command in existing candidate order. Candidate generation and physical saturation remain unchanged; no pruning based on saturation in just one outcome is performed.

The recurrence is:

```text
V[t,b,s] = max over commands a:
    sum over destinations j:
        P(duration)[s,j] * (step_score(t,b,a,j) + interpolate(V[t+1,:,j], next_battery))

V[final,b,s] = terminal_credit_rate * b
```

Only positive-probability terms participate. A command is rejected if any supported physical outcome or continuation is infeasible. Infeasible outcomes are never dropped and renormalised. Scheduled battery Min/Max remain soft penalties; equipment, capacity, import and battery-export limits remain hard.

The initial command comes directly from this expected-value policy, not from rescoring nine fixed scenario paths. `result_score()` returns `expected_score` for Markov results.

## Transition timing

Solar and load each use `dynamic_programming.transition_matrix`, with its reference duration in `transition_hours`. The shipped/default values are three hours and:

The Plan YAML editor preserves `uncertainty_model`, `transition_matrix` and `transition_hours` on round trips. The previous serializer omitted `uncertainty_model`, which could revert an edited demonstration or saved configuration to independent-scenario DP; that omission is corrected with matrix configuration support.

| From / to | Low | Medium | High |
| --- | ---: | ---: | ---: |
| Low | 0.6 | 0.3 | 0.1 |
| Medium | 0.2 | 0.6 | 0.2 |
| High | 0.1 | 0.3 | 0.6 |

`planning.markov_transitions.transition(hours, matrix, reference_hours)` applies the agreed scalar scaling to each row's total probability of leaving its current layer:

```text
q = sum of off-diagonal probabilities in the configured source row
scaled_exit = 1 - (1 - q) ** (hours / reference_hours)
P(source, destination) = scaled_exit * configured_probability / q   # destination != source
P(source, source) = 1 - scaled_exit
```

Off-diagonal destination proportions remain unchanged. A row with q = 0 stays in place. At zero elapsed time the matrix is the identity; if q = 1, every positive duration has exit probability one. `log1p`/`expm1` provide stable scalar evaluation for small positive probabilities. All positive durations are supported, including durations longer than the reference, without negative diagonal probabilities. At the reference duration the configured matrix is reproduced, within input row-sum tolerance.

The matrix must be 3-by-3 with finite probabilities in [0,1] and row sums within 1e-10 of one. Reference duration must be finite and positive. Singular matrices and identical rows are valid; no matrix logarithm, nonsingular requirement or SciPy calculation remains. Cache keys include the matrix and reference duration, so changed YAML cannot reuse stale transitions. Preparation uses actual UTC elapsed hours, including alignment tails and visible-boundary splits, outside battery/command loops.

This is row-wise exit-probability scaling, not a continuous-time matrix exponential. Repeated short transitions generally do not reproduce a single long transition: paths can leave and re-enter layers between decisions. Interval granularity therefore affects the projected condition process. The old composition guarantee and scalar spectral derivation in the original proposal no longer describe runtime behaviour.

Solar and load transitions are independent. Joint probabilities are their products in the explicit nine-layer order. At three hours, the probability of medium/medium ending medium/medium is 0.36. Old configured scenario probabilities are not multiplied into these transitions.

The default matrix at its three-hour reference duration has long-run probabilities `(2/7, 3/7, 2/7)`. Duration-scaled matrices can have different stationary distributions, and editing the matrix also changes them. The asymmetric load multipliers can make expected load differ from the medium forecast. Do not silently recenter probabilities or amounts.

Physical simulation uses one destination condition's aggregate energy for each entire interval. Coarser intervals approximate within-period ordering as well as changing the condition process under this scaling rule.

## Expected results and illustrative paths

`SimulationResult` retains `next_action`, nine `results` frames and nine `lookahead_results` frames. Markov results additionally contain:

| Field | Meaning |
| --- | --- |
| `expected_score` | Full configured-horizon expected objective at actual initial energy, medium/medium |
| `expected_results` | Visible-period expectations under the discretised policy |
| `expected_lookahead_results` | Remaining-period expectations, possibly an empty frame |

Non-Markov results leave these fields as `None`.

Forward evaluation starts with unit probability at the initial state. At every grid node it follows the backward policy, simulates each supported destination and splits ending probability between adjacent battery nodes using the same linear weights as value interpolation. The resulting period score sums, including terminal credit once at the final period, must agree with the backward expected score within numerical tolerance or calculation fails.

This evaluates the discretised model. It is not an exact continuously simulated distribution. Expected period frames expose solar, load, opening/ending battery, imports/exports, costs, protection/limit/terminal/total scores, expected hours below reserve, expected raw protection and supported ending-energy extrema. Extrema are not quantiles. No single operating mode or reconstructed source-to-destination routes are invented for these averages. Opposite-direction flows may both have positive expectations across different outcomes.

The nine traditional frames are fixed-condition stress illustrations. They all use the selected first command. Subsequent commands are selected at their exact battery energy using the coupled expected continuation, then simulated under the chosen fixed condition. They are not independent perfect-knowledge optimisations or an exhaustive set of Markov paths. Infeasible exact-state stress continuations fail explicitly rather than fabricating a report.

Positive force-charge controller targets use the largest first-step ending energy across stress outcomes; positive force-discharge targets use the smallest. These remain optional monitoring targets and do not change physical cut-offs or send commands.

## Consumers

The Plan tab displays expected visible net cost and full-horizon expected score separately. Existing solar/load selectors, charts and details retain the stress illustrations; their cost is labelled as stress-case cost. Excel detail adds a report-basis column. These stress totals must not be averaged with old scenario weights to reconstruct the Markov objective.

The as-of workbook adds an `Expected visible` sheet and summary rows identifying fixed-condition stress sheets, expected visible cost and full-horizon score. Grouping stress sheets sums score contributions as well as energy/costs. Expected rows retain model resolution even when stress sheets are grouped. CLI visible exports do not include hidden expectations; the full score includes them.

The existing `benchmark_planning --model dp` harness explicitly selects `independent_scenarios` because its audits and refinement experiments assume fixed paths. Its output records that mode. It does not benchmark Markov runtime or quality.

## Python and Numba

`use_numba` selects Python or lazily compiled versions of the same `backward_values()` and `forward_distribution()` functions. Shared helpers are registered for compilation. Periods, choices, grids, transitions and selected policy indices are converted to typed lists only for the compiled path. Native dispatchers are cached across calls; no synthetic warm-up, parallel loops or relaxed arithmetic are used.

Initial lazy compilation is included in the existing stage/total timing accounting. Markov solves report one full-range stage, plus an INFO summary containing the expected objective and identifying stress detail. The older DEBUG independent-scenario phase breakdown does not describe Markov phases.

## Approximation and remaining limits

The model assumes that future current conditions are observable at interval boundaries. Solar layers cannot actually be distinguished from solar measurements at night. Thus it removes fixed whole-future knowledge but is not a full hidden-state information model. Forecast recentering at the next real run is also not an explicit model of every possible future forecast update. These limits are recorded in [findings](../reviews/findings.md).

Other approximations include finite battery/action grids, value interpolation, probability splitting during forward evaluation, fixed uncalibrated transition assumptions and interval-average physics. Full-range search avoids unsafe old corridors but can cost more. No global continuous-model optimum or runtime claim is made.

Targeted tests in `tests/planning/test_markov.py` cover exit-probability scaling, expectation-before-command timing, small-tree exhaustive equivalence, cheap-rate charging, declining uneconomic charging, supported infeasibility, persistence, backend parity and public result assembly. No live database or installed-device verification is implied.

On 6 November 2026, targeted verification used the repository's Windows `.venv` interpreter. The Markov tests, selected configuration/Plan report tests, and key legacy DP pipeline/refinement/exhaustive-search regressions passed. An initial run with the unrelated system Python interpreter failed while importing native OSQP; verification then used `.venv`. No full suite, browser checks, model benchmarks or live database runs were performed. For focused rechecking, run `.\.venv\Scripts\python.exe -m pytest -q tests/planning/test_markov.py`; configuration and consumer coverage is in `tests/core/test_config.py` and `tests/dashboard/test_plan.py`.

## Saved plans

This branch builds on the database-backed Plan History feature. Shared reports preserve separate expected-result frames and score alongside all nine stress scenarios. The restored live summary shows expected visible net cost/full-horizon score and labels selected scenario costs as stress cases; both live and history Excel exports identify the report basis. Expected frames use the same configured retention period as Compact and Advanced, with expected-score and expected-visible-cost metadata retained alongside them. See [saved plans](plan-history.md).

The independent planning service also owns Markov runs; demonstrations use a separate worker. Historical snapshots are selected inside the single Plan tab. All retained snapshots keep both views and expected frames for the common configured period. Legacy Compact-only snapshots can still use expected visible net cost and full-horizon score from metadata; UI/export labels selected scenario details as stress cases.
