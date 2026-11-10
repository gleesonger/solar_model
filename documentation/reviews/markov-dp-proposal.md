# Proposal: battery planning with uncertain solar and load

Status: core implemented 6 November 2026; original design retained below. The [implementation contract](../implementation/markov-dynamic-programming.md) is the current reference.

Subsequent configuration change: the matrix and its reference duration now live in YAML as `transition_matrix` and `transition_hours`. On 7 November 2026, the requested scaling rule replaced matrix logarithms: apply `1 - (1 - q) ** (t / transition_hours)` to each row's total exit probability, retaining relative destination probabilities. Singular matrices are accepted and matrix-composition guarantees no longer apply. The fixed scalar spectral derivation below remains historical context only; the [implementation contract](../implementation/markov-dynamic-programming.md#transition-timing) describes current behaviour. UI path changes remain deferred while the user tests results.

This document records the agreed direction for replacing independent, perfectly informed scenario continuations with a coupled stochastic dynamic programme. It also identifies implementation recommendations and unresolved questions. The implemented version uses the fixed matrix and continuous-time duration conversion, multiplier-only layers, full-range search, separate expected reports, and the observable-condition approximation. The shipped YAML selects Markov; older configurations retain the independent-scenario compatibility mode. Remaining information limits are recorded in findings.

Current behaviour is described in the [DP walkthrough](../implementation/dynamic-programming.md) and [plan model](../overview/plan-model.md). Open issues are tracked in [review findings](findings.md).

## 1. Purpose

Choose the battery command to take now that maximises expected score over at least the next 24 hours, while allowing for imperfect knowledge of future solar generation and household demand.

For example, a sustained solar shortfall could leave the battery empty during an expensive evening tariff. The planner should consider that possibility when deciding whether to import during an earlier cheap period. It should weigh the benefit of avoiding expensive imports against the cost of charging unnecessarily, including conversion losses and the existing protection and limit scores.

Only the next command is selected for potential execution. Later decisions describe how the controller could respond as conditions become observable. Planning remains report-only; this proposal does not add device control.

## 2. What changes from the current DP

The current DP builds a separate backward value table for each complete solar/load scenario. It tests a shared first command, then allows each scenario its own continuation. Those continuations effectively know their scenario's remaining forecast path.

The proposed DP connects the nine layers through probabilistic transitions throughout the horizon. At every node, it evaluates the same candidate command across possible outcomes, averages their scores, and only then chooses the best command.

| Aspect | Current approach | Proposed approach |
| --- | --- | --- |
| Meaning of a scenario | A complete forecast path with an independently optimised continuation | A solar/load condition that can change probabilistically |
| Backward values | Independent tables | Nine connected layers of one value calculation |
| Command choice | Later choices depend on the known scenario path | Choices depend on current battery energy and available condition information |
| Uncertainty | Combined when comparing shared first commands | Included at every decision throughout the projection |
| Final decision | Best weighted score of completed fixed-scenario rollouts | Best expected score at the initial state |
| Future output | Nine scenario schedules | A policy, with distributions or illustrative paths for reporting |

The proposal retains the physical battery model, tariff handling, candidate policies and score components. It does not replace the objective with a manually chosen reserve rule.

## 3. Agreed modelling decisions

| Topic | Agreement |
| --- | --- |
| Graph | Time on x, battery energy on y, solar/load condition on z |
| Layers | Nine combinations of low/medium/high solar and low/medium/high load |
| Starting layer | Always medium solar / medium load, referred to as z = 0 |
| Solar baseline | Use the existing intraday-adjusted solar projection |
| Load baseline | Use the existing load forecast; do not scale future load from recent load actuals |
| Layer amounts | Use the current configured solar and load multipliers |
| Persistence | Use the supplied transition matrix for both solar and load |
| Matrix duration | One application represents three elapsed hours |
| Information order | Choose the command before the next uncertain outcome is revealed |
| Horizon | Consider at least 24 hours; retain the configured full horizon, currently 48 hours, unless separately changed |

The later instruction to use the matrix for load supersedes the earlier suggestion of independent load outcomes. There is no contradiction between keeping the real load forecast fixed and modelling persistent deviations from it within the projection.

Treating solar and load transitions as independent is a proposed initial simplification, not an empirically established property.

## 4. Forecast centring and layer definitions

### 4.1 Start each real calculation at the current forecast

At a real planning run, the intraday-adjusted solar forecast is the central solar path. The existing household forecast is the central load path. Current measured battery energy supplies the initial battery state.

The planner starts at medium/medium with probability one. This is a coordinate convention: medium means the current forecast reference, not certainty that its future amounts will occur.

Do not apply another actuals-based solar correction on top of the existing intraday correction. Do not infer a different initial load layer from past load actuals in this version.

At a subsequent real planning run, refreshed inputs define a new central forecast and a new z = 0. This recentring is a rolling-planning approximation; it is not evidence that every future forecast update has already been modelled exactly.

### 4.2 Layer amounts

For an interval, multiply the baseline solar energy by its solar layer multiplier, and baseline load energy by its load layer multiplier.

The current workspace configuration contains these values; implementation must read configuration rather than hard-code them:

| Dimension | Low | Medium | High |
| --- | ---: | ---: | ---: |
| Solar multiplier | 0.5 | 1.0 | 1.5 |
| Load multiplier | 0.7 | 1.0 | 1.5 |

The nine layers are the Cartesian product of these two dimensions. Use explicit dimension identities rather than relying on dictionary order or parsing informal display labels.

The existing scenario builder also redistributes high-case energy between paired half-hours. Whether to retain that timing stress is unresolved: it is separate from the agreed multipliers and can change the interpretation of a layer. A clean initial Markov model would use multiplier-only layers, subject to a deliberate implementation decision.

No percentile fitting or normal-distribution assumption is required for this version. Multipliers define energy amounts; transition probabilities define their likelihood and persistence.

## 5. The three-hour transition model

Use this matrix separately for solar and load. Rows identify the current condition and columns identify the destination condition:

| From / to | Low | Medium | High |
| --- | ---: | ---: | ---: |
| Low | 0.6 | 0.3 | 0.1 |
| Medium | 0.2 | 0.6 | 0.2 |
| High | 0.1 | 0.3 | 0.6 |

These are three-hour endpoint probabilities, not probabilities to apply once per DP interval regardless of its length. In particular, 0.6 is the probability of ending in the same condition after three hours, not necessarily the probability of never leaving it during those hours.

### 5.1 Convert to actual interval durations

DP periods can be 15 minutes, one hour or partial edge periods. Transition probabilities must use their actual elapsed duration.

The recommended interpolation is a continuous-time Markov model whose three-hour transition matrix is exactly the supplied matrix. For this particular matrix, an explicit scalar formula is available, avoiding a general matrix logarithm or eigendecomposition in the numerical loops.

Let h be the period duration in hours and u = h / 3. Define:

```text
S = [ [2/7, 3/7, 2/7],
      [2/7, 3/7, 2/7],
      [2/7, 3/7, 2/7] ]

A = [ [ 1/2, 0, -1/2],
      [   0, 0,    0],
      [-1/2, 0,  1/2] ]

B = [ [ 3/14, -3/7,  3/14],
      [ -2/7,  4/7,  -2/7],
      [ 3/14, -3/7,  3/14] ]

P(h) = S + (0.5 ** u) * A + (0.3 ** u) * B
```

This is matrix addition and scalar multiplication, not element-by-element exponentiation of the supplied probabilities. The eigenvalues of the supplied matrix are 1, 0.5 and 0.3; S, A and B are their corresponding projectors.

The formula has these required properties:

- P(0) is the identity matrix.
- P(3) is the supplied matrix.
- P(a) multiplied by P(b) equals P(a + b).
- Twelve 15-minute transitions reproduce one three-hour transition.
- Three hourly transitions reproduce one three-hour transition.
- Rows sum to one and entries are non-negative, allowing numerical round-off tolerance.

Prepare matrices once per distinct duration, outside the battery/action loops. Tiny floating-point deviations may be corrected within an explicit tolerance; material negative probabilities must fail validation.

This interpolation is a modelling recommendation beyond the agreed three-hour endpoints. Endpoint probabilities alone do not uniquely specify how conditions behave inside an interval. The period physical model still approximates the whole interval with one destination layer's energy; it does not simulate every within-period condition change.

Do not assume arbitrary future user-supplied matrices admit the same interpolation. A configurable replacement needs either a validated continuous-time construction or a separately defined timing model.

### 5.2 Combine solar and load transitions

For the initial independent-dimensions model:

```text
Pr(next solar = j, next load = k | current solar = s, current load = l)
    = P_solar(h)[s, j] * P_load(h)[l, k]
```

This produces a 9-by-9 matrix for each actual duration. The probability of both dimensions remaining in their starting layer after three hours is 0.6 * 0.6 = 0.36. Persistence of each dimension does not imply a 0.6 combined self-transition.

### 5.3 Existing probabilities are not extra weights

The Markov transition matrices replace the old fixed scenario weights for optimisation. Start from medium/medium with probability one and propagate that distribution. Do not multiply Markov transitions by the old scenario probabilities again.

The matrix's long-run distribution for each dimension is (2/7, 3/7, 2/7), not (0.2, 0.6, 0.2). Thus it does not preserve the old marginal scenario probabilities.

Medium is also not guaranteed to equal the expected energy amount. For example, the asymmetric load multipliers can make mean projected load exceed the medium load forecast. This is a consequence of the agreed inputs to document and assess, rather than silently normalising them away.

## 6. Information available at each decision

Random transitions remove knowledge of the future layer, but do not automatically resolve uncertainty about the current layer.

The proposed nine-layer DP assumes the current condition is observable at each future decision boundary. It does not assume that observing it reveals the remaining day's condition sequence.

Use this order consistently in backward calculation, policy evaluation and reports:

1. Start at time t with battery energy b and currently available condition s.
2. Choose one command a without seeing the destination condition.
3. Draw destination condition j using P for the interval duration.
4. Simulate the interval using j's solar/load amounts and the already chosen command.
5. Obtain the ending battery energy and period score.
6. At the next decision boundary, treat j as the newly observed condition.
7. Choose the next command using that condition and the new battery energy.

This is a boundary-based approximation: the interval outcome becomes available for the next command. The existing command simulator may respond physically to PV/load within the interval, but the chosen command cannot depend on a destination that has not yet been revealed.

### 6.1 Remaining observation limitation

Observable conditions are an approximation that must be explicit. At night, all solar multipliers produce zero solar, so actual solar cannot distinguish the layers. Similar ambiguity occurs when different layers produce indistinguishable measurements.

Giving future decisions access to a hidden solar layer could therefore retain an information advantage, even though the whole future path is uncertain. Nine condition layers alone do not solve that issue.

Before claiming a fully implementable information-limited policy, define when layers can be distinguished. Possible extensions are sharing decisions across indistinguishable layers, or tracking a probability distribution over hidden layers. The latter is a larger state-space change. An initial prototype may retain the observable-layer approximation, but must label it and test its effect, especially for overnight cheap charging.

## 7. Backward calculation

### 7.1 State and terminal values

Each node is (time boundary, stored battery energy, current solar/load condition). Battery energy remains measured in kWh from zero, with physical capacity and efficiencies unchanged.

Let V[t, b, s] be the best expected remaining score at that node. At the final boundary N:

```text
V[N, b, s] = b * terminal_credit_rate
```

Terminal credit uses the existing tariff and efficiency convention. It is included once, not also added independently to every earlier period.

### 7.2 Value one command

For command a and each destination condition j:

```text
step = simulate_period(device, period_inputs[t, j], b, a)
next_battery = step.battery_end

outcome_value = step.score
              + interpolate(battery_grid, V[t + 1, :, j], next_battery)

command_value = sum(P_joint(duration)[s, j] * outcome_value for j)
```

Choose the command with the highest command_value and store that value. Retain deterministic tie handling. The key ordering is:

```text
best command AFTER averaging its possible outcomes
```

Choosing the best command separately for every destination before averaging would reintroduce advance knowledge of that destination.

Ending energy is calculated by the physical simulator and can fall between nodes. Interpolate continuation value; do not round physical energy to a grid point.

### 7.3 Physical feasibility

Preserve all existing physical restrictions, including capacity, conversion efficiency, equipment power, grid-import and battery-export bounds. Scheduled Min/Max battery levels remain soft preferences with the existing bounded penalty.

Recommended initial rule: a command is eligible only if every positive-probability destination has a physically feasible step and feasible continuation. Do not discard adverse outcomes and renormalise the remaining probabilities: that changes the uncertainty model and conceals supply failures.

This is expected-score optimisation with hard feasibility across supported outcomes. It is not worst-case score optimisation, but rare supported outcomes can still constrain available commands. If this proves too restrictive, explicit risk or unmet-demand modelling would be a separate design change.

Skip zero-probability terms before handling infeasible values, avoiding expressions such as zero multiplied by negative infinity. Keep conservative interpolation behaviour around infeasible neighbours unless deliberately revised.

### 7.4 Select the action now

At the actual initial battery energy and medium/medium condition, evaluate candidate commands with the same expectation calculation and choose the highest value. Include the exact starting energy in the grid or evaluate its command values directly.

Do not choose the command by probability-weighting nine fixed-layer forward schedules. Those schedules no longer describe all the paths admitted by the model.

## 8. Score and why cheap charging can win

Retain the existing period score:

```text
period_score = -net_electricity_cost
               + battery_protection_reward
               + soft_battery_limit_score
```

Retain the exact nonlinear DP protection curve, tariff/preference scaling, actual-duration weights and final battery credit. Maximise the expected sum across the full configured horizon.

An expensive evening makes stored energy valuable when it can avoid future imports. The backward calculation carries that expected value to earlier times. Cheap-period charging wins when its expected benefit exceeds the current import and conversion costs, taking account of possible surplus solar, limits and the other score components.

Persistence allows sustained shortfalls to remain plausible instead of repeatedly cancelling low outcomes with independent high outcomes. It does not guarantee charging in every shortfall example: prices, probabilities, losses, capacity and terminal value still determine the result.

No additional arbitrary reserve target is required to produce this behaviour. The existing user protection preference remains part of the score, separately from the economic value of avoiding expensive imports.

## 9. Forward policy evaluation and reporting

The solution is a policy: a command choice for each time, battery level and available condition. It is not one committed schedule or nine exhaustive schedules.

For an illustrative path, start at medium/medium, choose the command using the policy, then reveal each subsequent condition and apply the corresponding physical step. A reporting path must not re-optimise using knowledge of its remaining trajectory.

Existing fixed low/medium/high paths can remain stress illustrations, but their weighted average is not the Markov model's expected score or expected bill. Reports need to distinguish:

- The selected next action and its ceilings.
- Expected full-horizon objective under the Markov model.
- Expected visible-horizon import cost and export revenue.
- Battery-energy distributions or selected illustrative paths.
- Diagnostic stress paths, if retained.

For a first deterministic report calculation, forward probability mass can be propagated through the battery/condition grid using the chosen policy and the same interpolation weights. This reproduces the discretised model's expectations when implemented consistently. Splitting mass between battery nodes is a numerical approximation, not an exact distribution of continuously simulated battery energies.

Exact-state sampled paths provide another reporting or validation method. They require a fixed random seed and uncertainty estimates when estimating expectations. They must not silently replace a deterministic objective with a noisy first-action comparison.

Report units and score meanings remain distinct: the objective includes subjective protection rewards and limit penalties; it is not an electricity bill. The initial action's optional controller SOC target also needs an explicit rule for multiple possible first-step outcomes.

## 10. Implementation scope

| Area | Proposed work |
| --- | --- |
| `planning/plan_optimisation_dynamic_programming.py` | Couple layer values, evaluate expectations before action selection, select the initial policy action, and evaluate reports consistently |
| `planning/scenarios.py` | Reuse configured multipliers with explicit solar/load identities; decide whether timing-stress redistribution belongs in Markov layers |
| `config.py` and configuration guides | Represent transition duration/matrices and model selection; validate dimensions, probabilities and supported duration conversion |
| `planning/types.py` | Represent expected objective/report metadata as needed; do not overload fixed scenario probabilities with a different meaning |
| `services/planning.py` | Reuse as-of adjusted solar and unchanged load forecasts; retain cutoff, vintage and UTC contracts |
| Dashboard and CLI report consumers | Label expectations and illustrative paths correctly; stop using old scenario weights for Markov summaries |
| Targeted planning tests | Check information order, transitions, economics, feasibility, reports and backend equivalence |

Keep planning independent of storage, network and UI. Prepare transition tables and period inputs outside numerical loops. Prefer lists, scalar arithmetic and loops, retaining the shared Python/Numba implementation approach.

Start with full-range battery grids. Existing refinement corridors follow one path per fixed scenario and can exclude states reached after layer changes. They cannot be carried over unchanged. A later refinement scheme would need to cover the reachable policy states, with acceptance based on the new expected objective.

Likewise, current command pruning needs review. Saturation in one destination does not justify skipping a higher rate if another positive-probability destination can still benefit. Pruning must preserve the command's complete outcome vector or have another demonstrated safe bound.

### 10.1 Complexity

For T periods, B battery nodes, K conditions and A actions, a direct backward pass is approximately O(T * B * K * A * K), with O(T * B * K) stored values. Here K = 9.

The value-table size is comparable to nine existing scenario tables. Work per command grows because it considers up to nine destinations. Transition-independent physical results can be reused across source conditions for the same time, battery and action.

The supplied matrix permits transitions between every pair for positive durations. Restricting transitions to neighbours would change the agreed model; it is not an exact performance optimisation. Runtime and compilation costs remain unmeasured.

## 11. Proposed implementation sequence

1. Define typed layer identities, period information order and transition preparation. Verify three-hour composition without running full model simulations.
2. Implement one full-range coupled backward pass and initial command selection in Python, reusing existing physics and scoring.
3. Check small exhaustive examples against the coupled DP, including explicit cases where seeing the next outcome would incorrectly improve the result.
4. Add the same numerical path to Numba and verify equivalent values and command choices.
5. Implement forward policy reporting and update result consumers so expectations cannot be confused with old scenario averages.
6. Retain the current planner as a comparison mode during validation. Choose activation and migration behaviour explicitly rather than silently changing old configuration meanings.
7. Consider refinement and measured performance work separately once correctness is established. Run benchmarks only when requested and apply the repository's timeout rules.

## 12. Targeted validation requirements

These are proposed checks, not completed verification:

| Check | Required evidence |
| --- | --- |
| Transition duration | Identity at zero, supplied matrix at three hours, non-negative normalised rows and composition across mixed/partial durations |
| Joint transitions | Product construction and row sums; fixed layer ordering |
| No advance outcome knowledge | Command selected before destination reveal; expected optimum differs from an intentionally clairvoyant counterexample |
| Initial centring | Start only at medium/medium using current battery energy; no second solar correction or actuals-based load scaling |
| Cheap-period charging | A small controlled case buys energy early when its expected later benefit warrants it |
| Unnecessary charging | A contrasting case declines charging when expected benefit is insufficient |
| Persistence | A small controlled case differentiates persistent errors from independent per-period outcomes |
| Physical constraints | Feasible steps conserve energy and respect equipment/site bounds in every supported destination |
| Infeasible branch | Positive-probability failures are not dropped or renormalised away |
| Scoring | Protection, limit, cost and terminal components agree between backward values and forward evaluation of the discretised policy |
| Deterministic reduction | With one reachable outcome, matches an equivalent deterministic finite-horizon calculation |
| Backend parity | Python and Numba agree within declared numerical tolerances and use the same tie order |
| Observation ambiguity | Nighttime solar layers cannot be presented as verified observable states; quantify or explicitly retain the approximation |
| Time contracts | Preserve exact horizon, first-action duration, UTC/DST arithmetic and actual-duration transition scaling |
| Reporting | Markov expectations are not calculated by old fixed-scenario weights; visible and lookahead components remain distinct |

Use temporary/synthetic inputs and targeted tests. This proposal requires no database changes or use of the user's live database.

## 13. Decisions still to resolve during implementation

1. Whether to retain the observable-layer approximation at night and other ambiguous times, or add an explicit information restriction before activation.
2. Whether multiplier-only Markov layers replace the existing high-case half-hour timing stress.
3. Whether the supplied matrix is fixed for the first version or configurable, and how unsupported alternative matrices are rejected.
4. Whether the continuous-time duration conversion proposed here is the adopted within-three-hour model.
5. The expected-result data contract, forward distribution method and controller SOC-target rule.
6. Activation defaults and how to preserve access to the current independent-scenario optimiser for comparison.

The agreement to implement a nine-layer Markov model does not establish calibrated probabilities, perfect layer observation, global optimality or improved real-world savings. Those remain modelling assumptions or validation questions.

## 14. Related material

- [Current DP implementation guide](../implementation/dynamic-programming.md).
- [Planning inputs and results](../implementation/planning.md).
- [Current plan model and scoring](../overview/plan-model.md).
- [Current forecast/scenario construction](../../solar_model/planning/scenarios.py).
- [Open planning limitations](findings.md).
- [Research example: stochastic DP for storage across a receding horizon](https://research.ibm.com/publications/optimal-operation-of-energy-storage-systems-considering-forecasts-and-battery-degradation). This establishes precedent for the general method, not validation of the specific transition matrix or implementation proposed here.
