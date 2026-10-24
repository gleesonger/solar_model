# Plan model

Proposed design, agreed on 2026-10-24. This document defines the target model; it does not describe an already implemented control system. Section 6 identifies the changes from current behaviour.

## 1. Summary

The system exists to choose **the next battery-control action to implement**, with a view of the coming day. Forecasts, physical modelling and optimisation support that decision.

One joint optimisation evaluates nine solar/load scenarios. All share the next 15-minute action, while their outcomes and later decisions can differ. A user preference balances expected financial return against retained battery protection. Only the next action is committed; the rest of the day is reconsidered at the next update.

| Section | Covers |
| --- | --- |
| [Plan Model](#2-plan-model) | How we choose the next action across nine possible futures and replan as observations arrive |
| [Scoring](#3-scoring) | How financial return, battery protection and the user's preference determine the winning plan |
| [Forecast Models](#4-forecast-models) | How solar and load forecasts produce the low, medium and high scenarios |
| [System Model](#5-system-model) | How battery behaviour, energy losses, operating modes and physical limits are represented |
| [Validation and Open Decisions](#6-validation-and-open-decisions) | How the model will be checked, what must change and what remains unresolved |

## 2. Plan Model

### 2.1 Overview

The plan model chooses the next 15-minute battery-control action to implement, considering its consequences over a rolling 24-hour horizon.

It evaluates nine combinations of low, medium and high solar and load forecasts. Every scenario shares the same next action. Scenario-specific outcomes and subsequent decisions can vary, subject to the system's physical constraints and command behaviour.

Each scenario is evaluated for electricity cost and retained battery protection. We take a weighted average of those outcomes, then use the user's preference to balance the relative cost and protection scores into one objective.

The optimiser selects the shared next action that maximises that objective. We implement that action, then update the measured state and available forecasts before solving again. The remaining day plans explain the decision and show possible outcomes; they are not committed schedules.

### 2.2 Joint Scenario Optimisation

We choose one action now by testing how it performs across nine possible futures. Later actions can adapt to whichever future unfolds.

Shared variables describe the first action: forced battery charge/discharge rates and their SOC cut-offs. Positive charging implies charge mode, positive discharging implies discharge mode, and both zero imply self-consumption. Each scenario has its own energy flows, battery path and later decisions.

The first interval's **physical outcomes are not shared**. The same self-consumption command can charge in a high-solar scenario and discharge in a high-load scenario. Those outcomes must follow the modelled command response; they cannot be freely chosen just to improve the score.

Construct the problem as nine blocks referencing one first-action object:

```text
shared_action = create_first_action()
blocks = [build_scenario(inputs, shared_action) for each scenario]
problem = combine(blocks.constraints, weighted_objective(blocks))
solve(problem)
```

Building a block creates variables, constraints and expressions; it does not solve a separate problem. The formulation remains a mixed-integer linear problem using CVXPY and HiGHS. Each reference or compromise optimisation includes all nine blocks.

Later decisions can adapt independently in each scenario. This two-stage approximation effectively assumes the scenario becomes known after the first interval; it is optimistic about future information. It is an initial design, not a guarantee against every possible future.

### 2.3 Next Action and Day Plan

The primary output is one implementable action for the next time step, with forced battery charge/discharge rates in kW and applicable SOC settings. Mode is derived from the rates rather than selected independently: charge rate positive means forced charge until charge SOC, discharge rate positive means forced discharge until discharge SOC, and both zero mean self-consumption ignoring these SOC cut-offs. Timing is implicit; the action has no timestamps. Net grid flow follows from battery power, load and solar. It carries four planning intents mapped to three physical modes:

| Intent | SigenStor mode | Intended behaviour |
| --- | --- | --- |
| Grid Import | 4: charge, PV first | Actively charge using PV first and grid supplementation |
| Grid Export | 5: discharge, PV first | Actively export using PV first and battery supplementation |
| Do Nothing: passive charge | 2: self-consumption | Allow surplus PV to charge the battery |
| Do Nothing: passive discharge | 2: self-consumption | Allow the battery to meet load when PV is insufficient |

The two passive intents send the same mode command. Actual PV and load determine the direction; the intent describes the expected movement. Mixed scenario expectations should be reported rather than forcing a single predicted direction.

Alongside the action, return a possible day plan for each scenario, weighted expected cost and protection, minimum usable battery energy, time below the preferred reserve, and the spread of outcomes. Targets and commanded limits are distinguished from predicted battery energy or grid flows, which vary by scenario.

### 2.4 Replanning and Execution

Only the next action is implemented. At each 15-minute update, initialise from measured battery state, refresh available inputs, rebuild the scenarios and solve again. Replanning does not imply refitting the fixed daily load forecast.

A separate controller applies the command, checks acknowledgement/read-back and monitors actual behaviour. It retains remote EMS control between updates. Returning to normal operation must also reconcile changed global limits; changing mode alone is insufficient. Stale inputs, failed solves or failed writes must be visible and trigger a defined handover policy rather than silently extending an old action.

The application currently has no battery-control writer. This document specifies the planning output and execution boundary; hardware activation is a later implementation step.

## 3. Scoring

### 3.1 Total Score and User Preference

We compare how much financial return we give up against how much extra battery protection we gain. The preference slider determines that balance, and the solver chooses the plan with the highest combined score.

The user supplies a preferred reserve, defined in section 3.3, and a protection preference $p$ from 0 to 100. The preference changes the balance between objectives, not the shape of the protection curve.

For each candidate joint plan, calculate weighted expected cost $C$ and protection $H$. Establish two reference plans using exactly the same scenarios, weights, starting state, constraints and horizon:

| Reference | Primary objective | Tie-break | Recorded values |
| --- | --- | --- | --- |
| Financial optimum | Minimise expected cost | Maximise protection | $C_0,H_0$ |
| Protection optimum | Maximise expected protection | Minimise cost | $C_1,H_1$ |

Normalise the candidate's position between these reference outcomes:

$$
F=100\frac{C_1-C}{C_1-C_0},\qquad
P=100\frac{H-H_0}{H_1-H_0}
$$

Then maximise:

$$
\boxed{J=\left(1-\frac{p}{100}\right)F+\frac{p}{100}P}
$$

$F$ measures financial performance relative to the reference cost range; $P$ measures protection improvement over the cheapest plan. A protection score $P=0$ does not mean an empty battery. It means no improvement over $H_0$. Normalisation happens after scenario aggregation, with one common set of reference values.

At preference 0, choose the financial reference; at 100, choose the protection reference. Interior preferences require the compromise solve. There are three logical optimisation stages; exact tie-breaking may require additional solver passes. If either reference range is zero within tolerance, avoid division and choose the solution achieving both objectives. Tiny ranges require numerical tolerances. Dominated candidates can fall outside the reference ranges; do not clip their scores in a way that changes the intended objective.

The following numbers are illustrative, not simulation results:

| Plan | Expected cost $C$ | Average protection $H$ | Financial score $F$ | Protection score $P$ |
| --- | --- | --- | --- | --- |
| A | EUR 10.00 | 40 | 100 | 0 |
| B | EUR 10.10 | 52 | 95 | 30 |
| C | EUR 10.40 | 64 | 80 | 60 |
| D | EUR 11.00 | 74 | 50 | 85 |
| E | EUR 12.00 | 80 | 0 | 100 |

Here $C_0=10$, $C_1=12$, $H_0=40$, and $H_1=80$. Combined scores are:

| User preference | A | B | C | D | E |
| --- | --- | --- | --- | --- | --- |
| 0 | **100** | 95 | 80 | 50 | 0 |
| 25 | 75 | **78.75** | 75 | 58.75 | 25 |
| 50 | 50 | 62.5 | **70** | 67.5 | 50 |
| 75 | 25 | 46.25 | 65 | **76.25** | 75 |
| 100 | 0 | 30 | 60 | 85 | **100** |

The solver searches feasible schedules, not five predefined plans. The same preference expresses a relative balance on different days; its euro impact can vary. Display the additional cost against the financial reference and the resulting energy trajectory, rather than presenting the preference as a percentage of protection or a spending limit.

### 3.2 Cost Score

Financial performance is expected net electricity cost over the horizon. For scenario $s$, let $I_{t,s}$ and $X_{t,s}$ be imported and exported energy in kWh, and $r_t^{buy}$, $r_t^{sell}$ the applicable rates in EUR/kWh:

$$
C_s=\sum_t\left(I_{t,s}r_t^{buy}-X_{t,s}r_t^{sell}\right),\qquad
C=\sum_s w_s C_s
$$

Losses are included in the physical energy flows. Cost can be negative when export revenue exceeds import cost. Fixed standing charges do not affect decisions; battery degradation cost is not included in the initial model. The financial score $F$ is derived from $C$ as defined in section 3.1.

### 3.3 Protection Score

Keeping energy matters most when the battery is nearly empty. Once we have a comfortable reserve, retaining more becomes progressively less valuable. We score the energy available rather than claiming a precise outage duration, since household behaviour and solar availability during an outage are uncertain.

Let $E_{t,s}\ge0$ be usable battery energy at interval end, above the physical discharge cut-off, and $R>0$ the user's preferred reserve in the same kWh convention. The agreed score is:

$$
S(E)=
\begin{cases}
100\left(1-0.3^{E/R}\right), & 0\le E\le R\\[4pt]
70+30\dfrac{E-R}{E+R}, & E>R
\end{cases}
$$

| Usable energy | Score |
| --- | --- |
| 0 | 0 |
| $R/2$ | 45.2 |
| $R$ | 70 |
| $2R$ | 80 |
| $3R$ | 85 |
| $5R$ | 90 |
| $10R$ | 94.5 |

The curve is continuous, increasing and concave, with a slope reduction at $R$. The reserve is entirely soft: falling below it loses score rather than making the optimisation infeasible. The score is independent of tariffs and is not an outage-survival probability.

Score each interval before averaging over time, then aggregate scenarios:

$$
H_s=\frac{\sum_t\Delta t_t S(E_{t,s})}{\sum_t\Delta t_t},\qquad
H=\sum_s w_s H_s
$$

$\Delta t_t$ is elapsed interval duration in hours. Equal 15-minute intervals reduce this to an ordinary average. Scoring the average battery energy would give a different result and hide low-energy periods. Even this time-averaged score does not guarantee a minimum reserve; display the minimum and time below $R$ separately.

Use a piecewise-linear approximation over the feasible energy range, with more breakpoints near zero and $R$. For segment lines $a_kE+b_k$, constrain interval score $q_{t,s}\le a_kE_{t,s}+b_k$ for every segment, and reward $q$. Concavity permits this with linear constraints and no extra binary variables. Chord interpolation understates the exact score between breakpoints. Use the same approximation in both references and the compromise solve; resolve score variables consistently when scoring a cost-only solution.

## 4. Forecast Models

### 4.1 Overview

Forecasts supply interval solar and load energy for the planning horizon. Produce each baseline once, then create variants using simple scalar multiplication; do not refit the models separately for each variant.

| Variant | Baseline multiplier | Initial weight in each dimension |
| --- | --- | --- |
| Low | 0.5 | $1/3$ |
| Medium | 1.0 | $1/3$ |
| High | 2.0 | $1/3$ |

Combine all solar/load pairs. Under the initial independence assumption, $w_{ij}=w_i^{solar}w_j^{load}$, giving each of nine scenarios weight $1/9$. Validate non-negative weights and normalise each dimension to one. These are adjustable planning weights, not calibrated probabilities or confidence intervals. There is no timing redistribution in this initial scenario model.

The existing forecast parameter names are `lookback`, `halflife`, and `prior_effective_observations`, under `forecast.model_calibration_parameters`. Despite its name, the last parameter is a decimal influence fraction $f$, not an observation count. Derived prior strength is $a=1/f-1$; evidence $n$ receives influence $n/(n+a)$.

### 4.2 Solar Day-ahead Forecast

We look at where the provider's forecast has historically been too high or too low at different times of day, and correct for that bias. This gives the day-ahead baseline before any same-day observations are used.

Select the last Forecast.Solar snapshot at or before local midnight; a new installation without one uses its first same-day snapshot. Preserve raw forecast history and collection/target timestamps.

For each UTC hour, fit a scaling factor through zero using earlier target days and recency weights $2^{-age/halflife}$. The factor is $\sum w\,raw\,actual/\sum w\,raw^2$; missing usable training gives factor 1. Interpolate factors around hourly centres and apply them to the site solar forecast, allocating output to panels by raw-power share.

Current YAML uses 42-day lookback and 14-day half-life; code defaults when settings are omitted are 56 and 21 days. `prior_effective_observations` is null because this model has no prior-shrinkage parameter. These are calibration inputs, not separate scenario controls.

### 4.3 Solar Intraday Adjustment

The intraday adjustment estimates whether today is producing more or less solar than the day-ahead forecast expected, then carries that tendency into the remaining forecast. If recent completed hours have underperformed, it lowers the forecast; if they have outperformed, it raises it.

Recent observations matter more than older ones. Differences are judged relative to the usual variation at each hour, so observations from different times of day can be combined. The adjustment is cautious when evidence is limited and gains influence as evidence accumulates. It estimates an above/below-forecast tendency rather than an explicit percentile.

Current YAML uses a 1-hour half-life, no additional lookback limit within today, and `prior_effective_observations: 0.3333`, giving one effective observation about one-third influence on the correction. Only completed hours contribute; the current incomplete hour is excluded. The correction applies to the current and remaining hours today, not future days. Without usable observations, the day-ahead forecast is retained.

### 4.4 Daily Load Forecast

We estimate demand from recent days, distinguishing weekdays from weekends. Where matching history is sparse, we borrow information from other days. The result is one hourly demand profile for the day, which stays fixed once issued.

Use the configured lookback and weights $2^{-age/halflife}$. For each clock hour, let $n$ be the sum of matching day-type weights. Blend the matching-type and all-days estimates with influence $n/(n+a)$. Without matching-type evidence use the all-days estimate; without any hourly evidence leave the prediction missing.

Current YAML uses 42-day lookback, 14-day half-life and influence fraction 0.2, giving prior strength 4. The first scheduled forecast run of a local day saves that day's profile atomically. It remains fixed across subsequent runs and restarts. There is no intraday load model. Stored UTC targets retain the 23/24/25 hours of the local day.

Rolling 24-hour planning also needs coverage beyond the current local day. Future-date load forecasts must use only observations available at issue time; their generation and freezing policy is an implementation decision recorded in section 6.3.

## 5. System Model

### 5.1 Overview

The system model determines whether a proposed action is physically achievable and predicts its effects under each scenario. Every scenario starts from the same measured battery state and device configuration, but receives different solar and load energy.

It represents battery storage, household demand, solar generation, the inverter and the grid connection. Hard constraints describe physical operating bounds; the preferred protection reserve remains a soft objective. No off-grid household load reduction is assumed: protection is measured in usable kWh, not promised hours of autonomy.

### 5.2 Operating Modes and Command Response

The executable control set is self-consumption (2), charge with PV first (4), and discharge with PV first (5). Modes 3 and 6 are excluded from this design. The numerical mapping is defined in Appendix 6 of the [Sigenergy Modbus V2.9 specification](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf); exact command response still requires verification on the installed firmware.

Derive mode from the forced battery rates, enforcing mutual exclusion and the applicable SOC stopping behaviour. Self-consumption ignores the action SOC cut-offs. Enforce the selected priority and saturation behaviour in every scenario, including the first interval. Shared commands do not guarantee identical realised charging, export or end SOC. Additional binary variables may be needed to model modes and limits; the existing charge/discharge boolean alone is insufficient.

Relevant plant registers are remote EMS enable `40029`, mode `40031`, charge/discharge power limits `40032`/`40034`, grid export/import limits `40038`/`40040`, and charge/discharge SOC cut-offs `40047`/`40048`. Plant address 247 returns acknowledgements. The V2.9 power-limit registers apply globally; power uses gain 1,000 and SOC cut-offs gain 10. Those protocol scales are separate from database scaling. Reading current state, writing only owned settings and defining handover are controller responsibilities.

### 5.3 Battery State, Energy Flows and Losses

We track where energy goes and how much remains in the battery, accounting for losses whenever it is charged or discharged.

Battery state is stored energy $B$, bounded by usable device capacity and physical cut-offs. Protection energy $E$ is the remaining usable portion above the discharge floor. Reconcile capacity, state of health, SOC and measured available energy before mapping between $B$, $E$ and SOC; do not mix their conventions.

Use interval kWh for all energy routes. With charge efficiency $\eta_c$ and discharge efficiency $\eta_d$, define solar/grid-to-battery flows as stored energy and battery-to-load/grid flows as delivered energy. Conservation is:

```text
solar = solar_to_load + solar_to_grid + solar_clipped + solar_to_battery / eta_c
load = solar_to_load + battery_to_load + grid_to_load
battery_end = battery_start + solar_to_battery + grid_to_battery
              - (battery_to_load + battery_to_grid) / eta_d
grid_import = grid_to_load + grid_to_battery / eta_c
grid_export = solar_to_grid + battery_to_grid
```

Carry each scenario's end energy into its next interval. The first starting state is measured, not a forecast. Do not allow simultaneous battery charging and discharging or assume that the same battery energy has a different physical identity because it came from PV or the grid.

### 5.4 Physical Constraints

The feasible plan must respect battery capacity and cut-offs, charging/discharging limits, shared inverter capacity, grid import/export limits and applicable device availability. Convert power limits to interval energy using elapsed hours and apply them at the correct AC/DC boundary, including losses. Available solar cannot be exceeded; allow explicit curtailment when it cannot be consumed, stored or exported.

Represent the grid meter's net-flow behaviour so simultaneous import/export cannot create artificial tariff profit. Physical limits remain hard; a desired protection reserve does not. Initial states outside modelled physical bounds, missing critical inputs or genuine infeasibility produce an explicit planning failure, not an executable action based on silently relaxed hardware constraints.

### 5.5 Units, Time and Data Conventions

Planning uses physical kWh, kW, hours, EUR/kWh and EUR. Database measurements are integers scaled by 10,000; ORM reads remove that scale, while raw SQL must divide explicitly. Forecast.Solar W/Wh also require their own unit conversion.

Use aware UTC instants and explicit start/end intervals, treating energy as belonging to `[start, end)`. Local dates, tariffs and daily forecast issuance use the configured timezone. A rolling 24-hour horizon is elapsed time; a local day can contain 23 or 25 hours. Resampling must conserve energy and preserve distinct repeated hours.

Only observations and forecast vintages available at the planning cutoff are eligible. Keep raw and adjusted history separate and preserve cumulative-counter reset handling. Unknown data must not silently become observed zero. Validate coverage, freshness, finite values and non-negative solar/load before constructing scenarios; any fallback must be explicit in the output.

## 6. Validation and Open Decisions

### 6.1 Acceptance Criteria

The implemented model should demonstrate:

- One identical first command across all scenarios, with differing first outcomes when inputs differ; later decisions can diverge.
- Exact 0.5/1/2 forecast scaling, normalised weights and reproducible weighted costs/protection.
- Score values matching section 3, correct preference endpoints and tie-breaking, safe zero-range handling, and bounded approximation error.
- A below-reserve initial state remains feasible whenever physical conditions permit; capacity, losses and power limits are never bypassed to meet protection preferences.
- Energy conservation, correct AC/DC boundaries, no artificial import/export arbitrage, and correct midnight/DST/as-of behaviour.
- Only the next action is committed, measured state replaces previous predictions on replanning, and failures are visible.

Use synthetic data and temporary databases for implementation tests. Before actuation, compare proposed commands and predicted outcomes with measured device behaviour in shadow operation.

### 6.2 Current Implementation and Required Changes

The code is organised in the main `solar_model` package. Its pure planning API, `planning.model.build_plan`, accepts in-memory inputs; `services.planning` assembles as-of database inputs and `tools.plan_as_of` produces the workbook. This structural migration preserves the existing algorithms; the target behaviour below is still to be implemented.

The current simulation uses shared first-step forced battery rate and SOC variables, and derives a local numeric `NextAction` and `OperatingMode` IntEnum after solving. The remaining command-response and scoring changes below are still required.

| Component | Current implementation | Required change |
| --- | --- | --- |
| Forecasts | `solar_model/planning/forecasts/` contains pure solar and load models; services load and persist their data | Reuse these baselines with complete planning-horizon coverage |
| Scenario generation | `solar_model/planning/scenarios.py` uses 0.5/1/1.5, with high-case timing changes and a separate temporary load profile | Use production baselines, 0.5/1/2 scalars and explicit dimension weights |
| Solver | [solar_model/planning/optimisation.py](../../solar_model/planning/optimisation.py) builds per-scenario scores/constraints and shared first controls, solves and extracts results within one function | Add PV-priority/saturation constraints and verify command response |
| Objective | Weighted net cost plus tariff-valued battery energy held over time | Replace with the reference-normalised cost/protection objective |
| Battery bounds | Configured minimum/maximum schedules are hard interval-end constraints | Separate physical bounds from the soft preferred reserve |
| Output | Per-scenario simulation frames and a workbook; numeric action/mode are local to the simulation | Integrate the next action and reference-normalised score diagnostics into the report/API |
| Execution | No device-control writer | Implement and validate the separate controller and handover policy |

### 6.3 Assumptions and Unresolved Decisions

The initial design assumes independent scenario weights, full scenario-specific flexibility after the first interval, and no calibrated outage probability. Protection uses stored energy rather than predicting behaviour during a power cut.

Before implementation is complete, settle the installed firmware's exact mode/limit response, battery energy/SOC conventions, tomorrow's load coverage and freezing policy, missing-data fallback, curve-approximation tolerance, and numerical tie-breaking tolerances. Before enabling writes, define command ownership, stale-plan handling and verified handover behaviour. The existing solar timestamp mismatch and potentially negative adjusted solar also need resolution at the planning boundary.

These implementation questions are tracked in [review findings](../reviews/findings.md). They do not change the agreed purpose: select and implement the next action using the day plan as decision context.
