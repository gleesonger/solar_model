# Plan model

Current planning and scoring contracts. The model produces reports and a proposed next action; the application does not send battery-control commands or automatically replan. Remaining implementation and hardware-verification work is separated in section 6.

Sources: [optimisation](../../solar_model/planning/plan_optimisation_milp.py), [scenarios](../../solar_model/planning/scenarios.py), [as-of inputs](../../solar_model/storage/queries/planning.py), [planning service](../../solar_model/services/planning.py), [configuration](../../solar_model/config.py), and [Plan tab](../../solar_model/dashboard/tabs/plan.py). See the [implementation guide](../implementation/planning.md) for field-level details.

## 1. Summary

The model chooses **a proposed next battery-control action**, with a view of the coming day. Forecasts, physical modelling and optimisation support that decision.

One joint optimisation evaluates all nine solar/load scenarios and chooses one shared next action: Sigenergy mode, both power ceilings and an optional controller SOC target. Outcomes and later decisions can differ by scenario. A user preference balances expected financial return against retained battery protection. Only the next action is intended for future execution; the remaining projection gives decision context. Today, both are report outputs.

| Section | Covers |
| --- | --- |
| [Plan Model](#2-plan-model) | How we choose the next action across nine possible futures and replan as observations arrive |
| [Scoring](#3-scoring) | How financial return, battery protection and the user's preference determine the winning plan |
| [Forecast Models](#4-forecast-models) | How solar and house-load forecasts produce low, base and high scenarios |
| [System Model](#5-system-model) | How battery behaviour, energy losses, operating modes and physical limits are represented |
| [Validation and Open Decisions](#6-validation-and-open-decisions) | What is implemented, what is checked and what remains unresolved |

## 2. Plan Model

### 2.1 Overview

The plan model proposes one next action over the complete configured elapsed horizon: `energy_plan.num_projection_hours` is currently 48, with `num_visible_projection_hours: 24` separating visible reports from lookahead. Both optimisers accept `projection_resolution_mins` arrays covering all 48 hours. MILP currently uses `[15*4, 60*23, 240*6]`; DP uses `[15*96, 60*24]`. Repetition entries expand during configuration loading. There is no hard-coded extra day.

MILP keeps the first action intact, clipping it at the visible/horizon cutoff if needed. Subsequent visible boundaries align from the preceding full UTC hour; overtaken boundaries are skipped and a final partial period preserves the visible duration. Configured steps after that portion start at the exact visible endpoint, so a 10:05 visible end still gives MILP lookahead starts at 10:05, 14:05, 18:05, 22:05, 02:05 and 06:05. DP uses elapsed durations from the projection start. Both evaluate tariffs and battery limits at interval starts and scale physical limits by actual duration. Reports/forecasts remain UTC, with configured-local conversion for tariffs, schedules and presentation.

It evaluates nine combinations of low, base and high solar and house-load forecasts. Every scenario shares the same next action. Scenario-specific outcomes and subsequent decisions can vary, subject to the system's physical constraints and command behaviour.

Each scenario is evaluated for electricity cost and retained battery protection across the visible horizon and a hidden 24-hour extension. We average the combined scores using scenario probabilities. The user's preference scales protection; a terminal credit values the final battery energy.

The optimiser selects the shared next action that maximises the probability-weighted objective. Each requested calculation initialises from as-of measurements and forecasts; there is no automatic execution/update loop. The remaining day plans explain the decision and show possible outcomes; they are not committed schedules.

### 2.2 Joint Scenario Optimisation

We choose one action now by testing how it performs across nine possible futures. Later actions can adapt to whichever future unfolds.

Five shared Boolean choices represent three policies: self-consumption, positive or zero-rate force charge, and positive or zero-rate force discharge. Exactly one is active. Shared variables describe positive forced rates; each scenario has its own energy flows, battery path and later decisions.

The first interval's **physical outcomes are not shared**. One shared policy and power profile is evaluated against each scenario's solar/load inputs. Forced power settings are ceilings: achieved power saturates at available PV/AC supply, inverter capacity, battery power and physical storage bounds. The optional controller SOC target is derived after solving; it does not curtail the first interval or change a device cut-off. Export limits constrain the shared command and resulting flows. The model assumes PV serves load before charging the battery, and PV output precedes battery output during discharge. Later decisions remain independently optimised. The response requires installed-firmware validation.

`simulate_energy_plan` builds and solves one joint CVXPY/HiGHS model. Six CVXPY quantity matrices represent usable PV, battery charge/discharge, grid import/export and battery energy. Forecasts, rates, limits and extracted report routes use ordinary Python lists. Each period's physical equations use scalar limits and one-dimensional scenario lists passed directly to CVXPY; rate lists over time feed each scenario's cost. First responses and report allocations use arithmetic and scalar min/max; named mode masks activate the corresponding equations. Two small helpers encode exact solver minimums and conditional equalities. There are no direct NumPy imports in the optimisation or physical/report-allocation modules. Later decisions remain independent by scenario.

There are no mode workers, candidate comparisons or process-pool startup costs. Exactly one fresh mixed-integer problem is compiled and solved with HiGHS, with warm starts disabled and no compiled cache. Solver-chosen mode flags determine the returned OperatingMode. Exact minima prevent discretionary under-delivery. Solver tolerances can select different nearly optimal schedules; no tie-break objective is added. Only optimal/optimal-inaccurate statuses are accepted; others fail the calculation.

Later decisions can adapt independently in each scenario. This two-stage approximation effectively assumes the scenario becomes known after the first interval; it is optimistic about future information. It is an approximation, not a guarantee against every possible future.

### 2.3 Next Action and Day Plan

The primary output is one implementable action for the next time step: a solver-selected Sigenergy mode, both ESS power ceilings and an optional controller SOC trigger. Positive forced rates use the configured minimum; zero-rate variants are separate candidate settings of the same modes. Timing is implicit. Achieved battery power and net grid flow depend on each scenario. These modes can be described by four planning intents:

| Intent | SigenStor mode | Intended behaviour |
| --- | --- | --- |
| Grid Import | 4: charge, PV first | Actively charge using PV first and grid supplementation |
| Grid Export | 5: discharge, PV first | Actively export using PV first and battery supplementation |
| Do Nothing: passive charge | 2: self-consumption | Allow surplus PV to charge the battery |
| Do Nothing: passive discharge | 2: self-consumption | Allow the battery to meet load when PV is insufficient |

The two passive intents correspond to the same mode value. Actual PV and load determine the direction; the intent describes the expected movement. Mixed scenario expectations should be reported rather than forcing a single predicted direction.

The pure API returns `SimulationResult(next_action, results, lookahead_results)`, where results maps scenario names to DataFrames of interval times, tariffs, limits, energy flows/losses, battery levels, costs and score contributions. The separate `lookahead_results` dictionary retains the configured hidden rows per scenario for diagnostics. Standard CLI reports and dashboard summaries use only `results`. Compact chart/table/download show that visible horizon. Advanced chart/table/download concatenate `results` and `lookahead_results`, showing the extra 24 hours; Advanced score totals therefore cover the full optimisation objective, while Compact/visible totals exclude lookahead contributions. The dashboard derives scenario-average/range cost, selected-scenario protection, minimum/end energy and time below reserve. Targets and commanded limits are distinguished from predicted battery energy or grid flows, which vary by scenario.

### 2.4 Replanning and Execution

The dashboard calculates on demand using Refresh or Apply in the YAML settings dialog, in a worker process. Model selection is energy_plan.optimiser.model. A successful run disables Refresh until another Apply starts a new run; it is initially enabled, and failures allow retry. Solar/Load scenario buttons only change the displayed outcome. There is no timer applying commands or recalculating the plan.

Each call builds and compiles a fresh problem; compiled-model caching and solver warm starts are disabled. The public wrapper owns overall timing/failure logging; the private function builds, solves and extracts results. Logs include start, compact timestep vector and complexity. Compilation, native solve and overall successful duration print to stdout with flushing.

Future execution would require a separate controller to apply/read back commands and define ownership, stale-plan handling and handover. No device-control writer or controller is implemented here.

### 2.5 Experimental dynamic programming

Both optimisers enforce the collected grid-point import limit (`grid_max_import_power_kw`, register 40040), including household demand and grid charging across visible/lookahead scenarios. Zero prohibits imports. Historical inputs without a reading remain unbounded. Insufficient battery/solar supply can make a plan infeasible. See the [DP import-limit contract](../implementation/dynamic-programming.md#grid-connection-import-limit).

`planning.plan_optimisation_dynamic_programming.simulate_energy_plan` is an alternative pure-calculation entry point with the same `SimulationResult` output. The dashboard Plan tab can select DP through the same as-of service; MILP remains the initial/default selection and the default for service/CLI calls that omit a model. DP uses its own complete duration array (currently 96 visible quarter-hours followed by 24 hourly lookahead periods) and nine weighted scenarios. Protection scaling, terminal credit and physical equipment rules follow the same definitions, implemented locally in DP. Scheduled battery Min/Max levels are soft preferences in DP and remain hard constraints in MILP. DP evaluates the exact nonlinear battery-protection formula directly; MILP retains its piecewise-linear approximation.

DP defaults to `use_numba: true`, with `false` selecting Python execution of the same original functions. Both live in the main DP module. Named tuples provide descriptive numerical fields; the compiled path converts existing lists to typed lists once per stage. `compiled_functions()` imports Numba locally only when enabled and reuses cached dispatchers across scenarios, stages and calls. There is no warm-up, so the first real call includes import/cache loading/compilation in its timing. Execution is single-threaded. Install with `python -m pip install -e ".[numba]"`; see the [backend contract](../implementation/dynamic-programming.md#python-and-numba-backends).

Periods are assembled separately for each scenario. Each interval resolves its configured-local tariffs, scheduled battery/export limits and duration, and integrates that scenario's solar/load forecast.

The coarse pass tests up to thirteen actions: self-consumption, a zero-rate variant of each forced policy, plus five positive rates per forced policy calculated as `minimum + (maximum - minimum) * fraction`, for fractions 1/5, 2/5, 3/5, 4/5 and 1. Each policy uses its configured minimum requested power and equipment-limited maximum. Duplicates are removed. The zero-rate variants remain available when no positive rate is valid. Scheduled Min/Max levels enter a bounded score rather than constraining flows; physical battery and equipment limits still apply. Future DP actions use the same executable-policy simulator.

The coarse backward value tables use a 2-percentage-point SOC grid (51 regular states), augmented with the measured initial energy and exact scheduled energy bounds. Linear interpolation estimates future value between states; actual battery energy is never rounded. Each candidate shared first command is then rolled forward at exact energy states, selecting later actions separately by scenario. Only complete feasible rollouts are eligible; the winner has the best realised probability-weighted score among those candidate rollout policies. This retains the current assumption of scenario-specific information after the first interval.

Optional per-period-end SOC search bounds allow scenario-specific corridors around an earlier path. They restrict the value grids and accepted rollout endpoints, while physical capacity and equipment/export limits stay in force. The default DP API runs 2% /5 full range, then 1% /10 at +/-5 points, 1% /20 at +/-3 points and 1% /40 at +/-1 point. Each stage centres its corridor on the best retained result. Infeasible or lower/equal-scoring stages are discarded. `energy_plan.optimiser.dynamic_programming` configures the grid and stages. See the [bounds and refinement walkthrough](../implementation/dynamic-programming.md#restricting-the-battery-search-by-period).

This is approximate dynamic programming, not a guaranteed optimum or a 2% economic-error guarantee. Coarse values can mis-rank actions; interpolation across an infeasible neighbour is rejected conservatively and can miss a feasible path. Schedule boundaries help but do not eliminate that limitation. A separate controller, firmware validation and the existing SOC-convention reconciliation remain outside the experiment. See the [current v1 verification and reproduction commands](../reviews/dp-v1-recheck.md), including the nine-scenario forced-command audit, and the [earlier comparison](../reviews/planning-dynamic-programming-comparison.md).

DP adds `limit_score = -100 * error**2 / (error**2 + 5**2) * period_hours`, where `error` is the interval-end breach in SOC percentage points of health-adjusted capacity (zero inside Min/Max). This is independent of protection preference and approaches -100 per hour. DP's total score includes cost, protection, limit and terminal components consistently in backward values, forward choices, scenario comparisons and reports. See the [DP scoring contract and code map](../implementation/dynamic-programming.md#soft-battery-limits-and-period-scoring).

DP and MILP evaluate the same three policy families. Mode 4 at zero charge holds both battery directions. Mode 5 at zero discharge prevents battery discharge while allowing surplus PV to charge. DP records the selected policy in each period; both optimisers return a complete mode and two-ceiling profile for the shared first action. These flow assumptions require installed-firmware verification. See the [command/profile contract](../implementation/dynamic-programming.md#program-commands-and-modbus-profiles).

## 3. Scoring

### 3.1 Total Score and User Preference

The tariff-scaled objective is implemented in `score_simulation`, which calls `score_scenario`. Both the objective and reported contributions use the same protection-value scaling through `_protection_value`.

In the formulas below, $t$ identifies a model interval and $s$ a scenario. The user supplies two inputs:

- **Target reserve R** (`preferred_reserve_kwh`), in stored battery kWh measured from zero, with R greater than zero. It controls the diminishing-return protection curve in section 3.3 and the reserve's replacement value. Do not add or subtract the physical discharge cut-off when interpreting R.
- **Protection preference X** (`protection_preference`) is any finite non-negative value (default 50), with no upper bound. It controls the strength of the protection reward. The user adjusts it by inspecting the cost and battery trajectory; it is not a percentage reserve requirement or a percentage cost increase.

For each scenario, add up net electricity cost and calculate its average battery-protection score over both visible and hidden periods. Convert protection into a monetary reward using the tariff plan, add terminal credit, subtract cost, then average scenario scores using their probabilities. Maximise this single objective; cost remains part of it at every X, including 100.

#### Tariff-based protection value

Sample the import tariff at projection start and then every elapsed hour for the next **24 hours**. Use the configured-local weekday and effective tariff plan at each timestamp, then average these 24 values. Changes between samples are deliberately approximated; this is a preference scale, not billing arithmetic. Do not weight by forecast consumption, cheapest charging opportunities or candidate-plan outcomes. Here $h$ counts elapsed hours, $r^{buy}$ is the import tariff, and $\bar r$ is their reference mean:

$$
\bar r=\frac{1}{24}\sum_{h=0}^{23}r^{buy}(\text{projection start}+h\text{ hours})
$$

Buying the target reserve R would cost $R\bar r$, using the reference mean just calculated. Apply the user's X and the projection duration $T=\sum_t\Delta t_t$ to obtain the maximum protection reward V in EUR:

$$
\boxed{V=\frac{X}{100}\,R\bar r\,\frac{T}{24}}
$$

A 24-hour projection uses the daily value directly; shorter/longer projections scale it proportionally because electricity cost accumulates over time while protection is averaged. R therefore changes both the preferred energy level and the monetary scale. No arbitrary maximum monetary reward is required.

Clip the reference mean to zero if it is non-positive. Such a tariff gives zero protection reward, avoiding a negative monetary multiplier; cost alone determines selection. Missing tariff coverage raises an error rather than inventing a rate.

#### Hidden lookahead

The configured total horizon includes the portion after `num_visible_projection_hours` as lookahead. With current settings this is 24 hours: six four-hour MILP periods or 24 hourly DP periods. Forecast solar and house load are integrated over each slot, with the same battery state carried across the visible/hidden boundary. Future costs and protection are scored normally. At the full horizon end (normally hour 48), terminal credit is remaining kWh times battery discharge and inverter DC-to-AC efficiencies times the mean of 12 hourly import/export midpoints over the following 12 hours, independent of X and without a load cap. With a 24-hour visible horizon, T is 48 hours in the protection scaling above. Scaling V with T while duration-weighting utility preserves the protection reward per elapsed hour.

The extension lets the model use battery energy for future load, replenish it through future solar/grid charging and export it later. Terminal credit reduces end-of-horizon depletion incentives without imposing a final reserve.

The four-hour slots deliberately do not align to tariff boundaries. Each slot uses its starting import/export rates and battery limits for all four hours, so changes inside it are approximated. Solar/load energy totals also hide their ordering within a slot, and one battery direction applies throughout each slot. These are accepted coarse-model approximations, not a detailed second-day schedule. The hidden day's solar repeats today's hourly profile by configured-local clock hour: measured PV power through the as-of instant, then adjusted forecast for the rest of today. Partial hours blend actual and forecast energy. This supplies the hidden day even when the provider snapshot stops earlier; the nine solar scenarios then scale/stress this repeated baseline normally.

#### Scenario score and combined objective

Use V calculated above with scenario net cost $C_s$ ([section 3.2](#32-cost-score)) and time-averaged raw protection $H_s$ ([section 3.3](#33-protection-score)). This gives scenario score $J_s$. The probabilities $w_s$ are non-negative and sum to one; their weighted average gives the overall objective J:

$$
J_s=-C_s+V\frac{H_s}{100}+W_s,\qquad
\boxed{J=\sum_s w_s J_s=-C+V\frac{H}{100}+W}
$$

W is the probability-weighted terminal credit. Here C and H are the same probability-weighted averages of $C_s$ and $H_s$, using the weights $w_s$. The numerical score has EUR units but includes a subjective protection reward. It is not an electricity bill, profit forecast or expected payout. Report net electricity cost and protection separately alongside the chosen plan.

- **X = 0:** minimise expected net cost less terminal credit across visible and hidden periods; protection has no influence.
- **Increasing X:** pay progressively more attention to retaining energy, using the same tariff-based scale.
- **X = 100:** use the full tariff-scaled reserve replacement value; cost still matters.
- **X > 100:** increase the reward scale beyond that replacement value; for example X = 200 doubles it. The raw utility curve remains capped at 100, but its monetary scale V grows with X.

There are no cheapest/protection-first reference solves, no division by an achievable min/max range and no hard extra-spending allowance. Small protection improvements remain small. Relative to the optimum at X = 0, the sacrifice in expected net cost less terminal credit cannot exceed V under an optimal solve. Spending V is not a target: any sacrifice must be justified by the actual protection gain, V times the change in H divided by 100. This bound includes both horizons and terminal credit, not the visible report alone.

#### Example

Ignoring terminal credit in this illustration, for R = 4 kWh, average import rate EUR 0.30/kWh, X = 50 and T = 48 hours (24 visible plus 24 hidden):

- Reserve replacement value: EUR 1.20.
- Maximum protection reward V: EUR 1.20.
- Holding R throughout the horizon scores 70 and earns EUR 0.84; holding 2R scores 80 and earns EUR 0.96; holding 3R scores 85 and earns EUR 1.02.

Moving from R to 2R justifies at most EUR 0.12 extra cost. Moving from 2R to 3R justifies only EUR 0.06 extra. These values are rewards for entire trajectories with those average protection scores, not per-interval payments.

The following candidate outcomes are illustrative, not simulation results,:

| Plan | Expected net cost C | Average protection H | Protection reward | Combined score J |
| --- | --- | --- | --- | --- |
| A | EUR 10.00 | 70 | EUR 0.84 | -9.16 |
| B | EUR 10.10 | 80 | EUR 0.96 | **-9.14** |
| C | EUR 10.20 | 85 | EUR 1.02 | -9.18 |

B wins: its protection improvement over A is worth more than the extra cost, while C's further improvement is not. The optimiser searches all feasible schedules, not these three predefined outcomes. Protection continues increasing above R, but the additional benefit diminishes; it never penalises energy merely for being above reserve.

#### Advanced result columns: post-scaling contributions

For one selected scenario, the formula above is split into period contributions. `net_cost[t]` is that period's import cost minus export revenue; `q[t]` approximates the raw curve $S(E_{t,s})$ from section 3.3. V is the same tariff-scaled reward above, `duration[t]` is $\Delta t_t$, and `total_duration` is T:

```text
cost_score[t]       = -net_cost[t]
protection_score[t] = V * q[t]/100 * duration[t]/total_duration
terminal_score[t]   = terminal credit on the final hidden row, otherwise zero
total_score[t]      = cost_score[t] + protection_score[t] + terminal_score[t]
```

`q[t]` is recomputed from solved end energy using the same piecewise-linear envelope as the optimiser, not arbitrary auxiliary-variable slack. These columns are **post-scaling**: protection already includes X, reserve/tariff value and duration weighting. Cost is simply negative net cost, without another scalar. Their time sums are the selected scenario's scores; scenario probability is applied only when combining scenarios, not inside these columns. Probability-weighted scenario totals reproduce the objective within solver tolerance only when visible and hidden contributions are both included.

Advanced detail divides cost, protection and limit contributions by their interval duration in hours for display. Terminal score is a one-time credit, and Total score includes it, so those two cells show their original period contributions without division. The **hours** column shows elapsed duration (15 minutes = 0.25). Its footer shows contributions for the first 24 hours, the remaining horizon and the full Total. An interval crossing 24 hours is split proportionally, except terminal score, which belongs to the interval end. Excel retains the original period contributions, includes the same footer split and omits the display-only hours column. Scores and hours display to two decimals. Compact detail omits score columns. The dashboard's separate Average protection summary reports the exact nonlinear 0-100 curve, which can be slightly higher than the LP approximation used by these contributions.

### 3.2 Cost Score

Financial performance is expected net electricity cost over the horizon. For scenario $s$, $I_{t,s}$ and $O_{t,s}$ are the interval fields `grid_import_kwh` and `grid_export_kwh`; $r_t^{buy}$ and $r_t^{sell}$ are `import_rate` and `export_rate` in EUR/kWh. Sum their net cost over intervals to obtain $C_s$, then use the same scenario weights $w_s$ to obtain C:

$$
C_s=\sum_t\left(I_{t,s}r_t^{buy}-O_{t,s}r_t^{sell}\right),\qquad
C=\sum_s w_s C_s
$$

Losses are included in the physical energy flows. Cost can be negative when export revenue exceeds import cost. Fixed standing charges do not affect decisions; battery degradation cost is not included in the initial model. Cost enters the objective directly as $-C$; it is not normalised against reference plans.

### 3.3 Protection Score

Keeping energy matters most when the battery is nearly empty. Once we have a comfortable reserve, retaining more becomes progressively less valuable. We score the energy available rather than claiming a precise outage duration, since household behaviour and solar availability during an outage are uncertain.

Let $E_{t,s}\ge0$ be stored battery energy at interval end, measured from zero, and $R>0$ the user's preferred reserve on that same scale. Score the stored energy directly, without subtracting the physical discharge cut-off. For example, R = 4 kWh means 4 kWh stored, even if the discharge cut-off corresponds to 2 kWh. Configured minimum/maximum battery bounds remain separate hard feasibility constraints in MILP; DP uses its separate bounded limit penalty. In the curve below, E means one interval's $E_{t,s}$ (`battery_end_kwh`), and $S(E)$ is its raw 0-100 protection utility:

$$
S(E)=
\begin{cases}
100\left(1-0.3^{E/R}\right), & 0\le E\le R\\[4pt]
70+30\dfrac{E-R}{E+R}, & E>R
\end{cases}
$$

| Stored energy | Score |
| --- | --- |
| 0 | 0 |
| $R/2$ | 45.2 |
| $R$ | 70 |
| $2R$ | 80 |
| $3R$ | 85 |
| $5R$ | 90 |
| $10R$ | 94.5 |

The curve is continuous, increasing and concave, with a slope reduction at $R$. It approaches 100 asymptotically and cannot reach 100 at finite stored energy; 100 is a bounded utility scale, not 100% SOC or a survival probability. The LP also explicitly constrains each auxiliary utility to `0 <= q <= 100`. Bounded utility limits the total protection reward to V for the chosen X; because X is unbounded above, V is not capped at the reserve replacement value. The scaled `protection_score` column is not itself a raw 0-100 utility. The reserve is entirely soft: falling below it loses score rather than making the optimisation infeasible. The score is independent of tariffs and is not an outage-survival probability.

Apply $S(E)$ to each interval's end energy first. Average those utilities using elapsed durations $\Delta t_t$ to obtain $H_s$, the scenario protection value used in section 3.1; average again with the same scenario weights $w_s$ to obtain H:

$$
H_s=\frac{\sum_t\Delta t_t S(E_{t,s})}{\sum_t\Delta t_t},\qquad
H=\sum_s w_s H_s
$$

$\Delta t_t$ is elapsed interval duration in hours. Equal 15-minute intervals reduce this to an ordinary average. Scoring the average battery energy would give a different result and hide low-energy periods. Even this time-averaged score does not guarantee a minimum reserve; display the minimum and time below $R$ separately.

The implementation uses a concave piecewise-linear chord approximation over health-adjusted capacity, refining segments until maximum utility shortfall is at most the fixed `PROTECTION_APPROXIMATION_ERROR = 0.05` points. For chord segment k, $a_k$ and $b_k$ are its slope and intercept. The LP utility $q_{t,s}$ replaces exact $S(E_{t,s})$ in the average above: constrain $q_{t,s}\le a_kE_{t,s}+b_k$ for every segment, and reward q. Concavity permits this with linear constraints and no extra binary variables. Chord interpolation understates the exact score between breakpoints. Use this approximation in the single joint-scenario solve. At X = 0, protection variables are not rewarded and can sit below the curve; recompute reported protection from the solved energy trajectory rather than reporting arbitrary slack values.

## 4. Forecast Models

### 4.1 Overview

Forecasts supply interval solar and house-load energy for the planning horizon, excluding conversion losses already modelled separately. Produce each baseline once, then create variants using simple scalar multiplication; do not refit the models separately for each variant.

| Variant | Baseline multiplier | Initial weight in each dimension |
| --- | --- | --- |
| Low | 0.5 | 0.2 |
| Med | 1.0 | 0.6 |
| High | 1.5 | 0.2 |

The required `energy_plan.scenarios.solar` and `energy_plan.scenarios.house_load` mappings configure low/med/high entries with explicit multipliers and probabilities (shipped values above). `create_scenarios(forecast, config.energy_plan.scenarios)` combines every load/solar pair and assigns each of nine scenarios the product of its configured dimension probabilities, assuming independence. These are planning assumptions, not calibrated probabilities. The pure solver API also accepts custom `ScenarioData` probabilities, requiring finite non-negative weights with positive total and normalising the combined objective.

The generator first samples contiguous whole-hour coverage into paired half-hours. Low/med preserve those half-hour proportions. High load scales each hour by its configured multiplier and redistributes 80% into its first half-hour and 20% into its second; high solar uses 25% then 75%. Thus the high cases change timing as well as totals. Names are `load_solar` (for example `low_high`), except both med is named `med`.

The existing forecast parameter names are `lookback`, `halflife`, and `prior_effective_observations`, under `forecast.solar.model_calibration_parameters.day_ahead`/`intraday` and `forecast.load.model_calibration_parameters`. Despite its name, the last parameter is a decimal influence fraction $f$, not an observation count. The configured fraction f gives prior strength $a=1/f-1$; evidence n then receives influence $n/(n+a)$. Here n is the matching-day weight sum for load, or the effective sample size for solar intraday.

### 4.2 Solar Day-ahead Forecast

We look at where the provider's forecast has historically been too high or too low at different times of day, and correct for that bias. This gives the day-ahead baseline before any same-day observations are used.

Select the last Forecast.Solar snapshot at or before local midnight; a new installation without one uses its first same-day snapshot. Preserve raw forecast history and collection/target timestamps.

For each UTC hour, fit a scaling factor through zero using earlier target days. With age and configured `halflife` both in days, each observation has weight $w=2^{-age/halflife}$. Here raw is the provider forecast power and actual is measured PV power, both in kW. The factor is $\sum w\,raw\,actual/\sum w\,raw^2$; missing usable training gives factor 1. These observation weights w are separate from scenario probabilities $w_s$. Interpolate factors around hourly centres and apply them to the site solar forecast, allocating output to panels by raw-power share.

Current YAML uses 42-day lookback and 14-day half-life; code defaults when settings are omitted are 56 and 21 days. `prior_effective_observations` is null because this model has no prior-shrinkage parameter. These are calibration inputs, not separate scenario controls.

### 4.3 Solar Intraday Adjustment

The intraday adjustment estimates whether today is producing more or less solar than the day-ahead forecast expected, then carries that tendency into the remaining forecast. If recent completed hours have underperformed, it lowers the forecast; if they have outperformed, it raises it.

Recent observations matter more than older ones. Differences are judged relative to the usual variation at each hour, so observations from different times of day can be combined. The adjustment is cautious when evidence is limited and gains influence as evidence accumulates. It estimates an above/below-forecast tendency rather than an explicit percentile.

Current YAML uses a 1-hour half-life, no additional lookback limit within today, and `prior_effective_observations: 0.3333`, giving one effective observation about one-third influence on the correction. Only completed hours contribute; the current incomplete hour is excluded. The current target day uses intraday residuals for the current and remaining hours. Future target days receive the calibrated baseline without today's residual adjustment. Without usable observations, the calibrated forecast is retained.

### 4.4 Daily Load Forecast

We estimate demand from recent days, distinguishing weekdays from weekends. Where matching history is sparse, we borrow information from other days. The result is one hourly demand profile for the day, which stays fixed once issued.

Use the configured lookback and recency weights $2^{-age/halflife}$, with age and half-life in days. For each clock hour, n is the sum of matching weekday/weekend weights; the configured influence fraction f gives $a=1/f-1$ as in section 4.1. Blend the matching-type and all-days estimates with influence $n/(n+a)$. Without matching-type evidence use the all-days estimate; without any hourly evidence leave the prediction missing.

Current YAML uses 42-day lookback, 14-day half-life and influence fraction 0.2, giving prior strength 4. The first scheduled forecast run of a local day saves that day's profile atomically. It remains fixed across subsequent runs and restarts. There is no intraday load model. Stored UTC targets retain the 23/24/25 hours of the local day.

Planning uses the same production `house_load.predict_day()` helper and `forecast.load.model_calibration_parameters` settings, but recomputes a profile for each required local target date rather than consuming the persisted daily profile. It reads current holiday periods on each calculation: ordinary weekday/weekend profiles exclude holiday observations; holiday targets use the separate holiday model with a 30-day flat baseline and half-length holiday-count lookback/half-life. Every target date sees history only before the as-of local day's midnight, so no current-day/future observations leak into tomorrow's projection. Missing predicted hours raise an error. The unused energy-plan load-history fields have been removed.

`load_forecast_as_of` selects the latest provider collection at or before as-of, preferring complete adjusted panel cumulative-energy fields and otherwise raw fields. It converts cumulative Wh into kWh per target using configured-local reset days. The planner currently treats forecast target timestamps as interval ends; this differs from dashboard interpretation and remains a [review finding](../reviews/findings.md). The visible forecast retains its existing leading/trailing zero-padding policy. For the hidden extension, `_lookahead_solar` builds today's actual-plus-forecast hourly mean-power profile and repeats it by local clock hour, replacing the provider tail. Actuals use only samples known by as-of; an elapsed hour without samples falls back to its saved forecast and logs a warning. ORM PV kW samples are averaged within each hour; the elapsed/future fractions of an open hour use actual/forecast respectively. Repeated autumn source hours are averaged over their elapsed duration; a skipped spring source hour contributes zero. Solar and house-load coverage surround the visible projection plus the 24-hour extension at full UTC hours so nine scenarios can be built; the model integrates its actual requested intervals. These are explicit input-assembly policies, not ForecastData extrapolation.

`forecast.solar` groups provider endpoint, timeout, scan interval, array definitions and day-ahead/intraday calibration. `forecast.load` groups house-load calibration. Dashboard PV-string mapping uses unique array names, resolved to stable panel IDs for storage queries.

## 5. System Model

### 5.1 Overview

The system model determines whether a proposed action is physically achievable and predicts its effects under each scenario. Every scenario starts from the same measured battery state and device configuration, but receives different solar and load energy.

It represents battery storage, household demand, solar generation, the inverter and the grid connection. Hard constraints describe physical operating bounds; the preferred protection reserve remains a soft objective. No off-grid household load reduction is assumed: protection scores stored kWh from zero, not promised hours of autonomy.

### 5.2 Operating Modes and Command Response

The executable control set is self-consumption (2), charge with PV first (4), and discharge with PV first (5). Modes 3 and 6 are excluded from this design. The numerical mapping is defined in Appendix 6 of the [Sigenergy Modbus V2.9 specification](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf); exact command response still requires verification on the installed firmware.

MILP selects among three Sigenergy policies, including zero-power variations of modes 4 and 5. The first interval follows the assumed PV-to-load, surplus-to-battery, then export order, and PV before battery for load. Positive force-charge and force-discharge rates are ceilings constrained by physical supply, power limits and site/battery-export limits. A controller SOC target is derived after solving, not applied as an inverter stop. User minimum/maximum energy schedules remain hard interval-end bounds in MILP and soft score preferences in DP.

This is a declared response model, not firmware verification. The active mode persists for the interval; power and energy are averaged uniformly. Ramping, response delay and subinterval clipping are not simulated.

The [Sigenergy Modbus V2.9 specification](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf) and [register review](../reviews/sigenergy-home-assistant-register-review.md) provide integration context. No control-register writes, acknowledgement/read-back, retained EMS ownership or handover implementation exist in this application.

### 5.3 Battery State, Energy Flows and Losses

We track where energy goes and how much remains in the battery, accounting for losses whenever it is charged or discharged.

Battery state is stored energy $B$, with non-negative interval ends bounded by health-adjusted capacity. MILP additionally enforces configured minimum/maximum energy schedules; DP scores breaches of those schedules. Protection scores this same stored energy: $E=B$, with zero as the origin for both E and R. The discharge cut-off does not shift the scoring origin. The input loader and dashboard SOC conversion use nameplate capacity times SOC/100; physical capacity and command-stop conversion use capacity times state of health/100. This mismatch needs reconciliation when health is below 100%; available-discharge telemetry has a different meaning and is not substituted. Metadata charge/discharge SOC cut-offs are not automatically added as hard energy bounds.

The solver uses interval kWh for PV energy U at the DC input, battery charging input C at the DC port, battery discharge output D at the DC port, grid charging input G at the inverter AC port, stored battery energy B, and grid import/export. Four separate efficiencies apply: inverter AC-to-DC $\eta_{acdc}$, inverter DC-to-AC $\eta_{dcac}$, battery DC-to-DC charge $\eta_c$, and battery DC-to-DC discharge $\eta_d$. PV is assumed available at the DC input without an additional PV-to-bus loss:

```text
battery_solar_charge = C - eta_acdc * G
gross_ac_output = eta_dcac * (U - battery_solar_charge + D)
inverter_net_ac = gross_ac_output - G
battery_end = battery_start + eta_c * C - D / eta_d
grid_export - grid_import = inverter_net_ac - household_demand
```

Source-to-destination routes are reconstructed for reports: PV charges first, then grid; remaining PV serves household load before battery discharge; excess AC is exported. The four explicit loss columns are `inverter_ac_to_dc_loss_kwh = G(1-eta_acdc)`, `inverter_dc_to_ac_loss_kwh = (U-battery_solar_charge+D)(1-eta_dcac)`, `battery_charge_loss_kwh = C(1-eta_c)`, and `battery_discharge_loss_kwh = D(1/eta_d-1)`. These are model allocations, not separately measured flows.

The resulting `battery_end` is $E_{t,s}$ used by the protection curve; `grid_import` and `grid_export` are $I_{t,s}$ and $O_{t,s}$ used by the cost formula. Carry each scenario's end energy into its next interval. The first starting state is measured, not a forecast. Do not allow simultaneous battery charging and discharging or assume that the same battery energy has a different physical identity because it came from PV or the grid.

### 5.4 Physical Constraints

Battery charging input from solar plus grid is bounded by battery DC charging power times interval hours. Gross inverter AC output respects its active output limit; grid charging input respects AC absorption and site import limits. AC absorption does not cap DC solar charging.

Implemented constraints include health-adjusted capacity, configured hard minimum/maximum battery schedules, mutually exclusive charging/discharging, duration-scaled battery limits, signed inverter AC output/absorption bounds and the configured battery-to-grid export limit. The preferred reserve R is separate and soft. Current power settings constrain battery/inverter routes; they do not establish general site import/export meter limits.

Solar may be explicitly clipped when it cannot be used, stored or exported. Flow equalities conserve solar, household load and battery energy. Import/export flows are mutually exclusive within each interval and scenario. The same shared action can import in one scenario and export in another. The collected grid-point import cap limits imports; output capability limits exports.

All three first-step modes follow the declared scenario-specific physical response, including PV priority and forced-power saturation. Configured energy/export limits reject unsafe commands rather than reducing their predicted response; energy stops are encoded in the active command, and restrictive export limits bound its discharge rate. Self-consumption is unavailable when its passive response would breach user energy limits. The battery-only export cap applies to the reconstructed battery export `min(delivered battery output, grid export)`, independently of solar exports. Hardware response and SOC-convention questions remain tracked in [review findings](../reviews/findings.md). Infeasible solves raise an error; initial measured energy is not itself constrained by interval-end minimum/maximum schedules.

### 5.5 Units, Time and Data Conventions

Planning uses physical kWh, kW, hours, EUR/kWh and EUR. Database measurements are integers scaled by 10,000; ORM reads remove that scale, while raw SQL must divide explicitly. Forecast.Solar W/Wh also require their own unit conversion.

Use aware UTC instants and explicit start/end intervals, treating energy as belonging to `[start, end)`. Local dates, tariffs and daily forecast issuance use the configured timezone. The visible 24-hour projection ends 24 elapsed hours after its starting instant, including a final partial period when necessary; the configured lookahead durations complete the 48-hour total horizon (six four-hour MILP steps or 24 hourly DP steps). A local day can contain 23 or 25 hours. Resampling must conserve energy and preserve distinct repeated hours.

Only observations and forecast vintages available at the planning cutoff are eligible. Keep raw and adjusted history separate and preserve cumulative-counter reset handling. Unknown data must not silently become observed zero. ForecastData validates aware intervals, ordering, contiguity and requested coverage, and the model validates scenario names/probabilities. The solver rejects non-finite numeric inputs and negative solar/load energy or battery export limits. Input-age rejection is not implemented; a valid as-of snapshot can still be old. Solar fallback/padding and missing-house-profile behaviour are described in section 4.4 and remain data-policy limitations.

## 6. Validation and Open Decisions

### 6.1 Verified Behaviour and Checks

Targeted planning and dashboard checks exercise the shared first action, policy rates, energy conservation, grid limits and presentation. Synthetic inputs and temporary databases are used; numerical checks do not establish installed-firmware response.

Scoring checks cover preference endpoints, zero-based reserve, non-positive reference tariffs, horizon scaling, probability normalisation, bounded chord error, actual-plus-adjusted solar repetition, as-of leakage and DST checks, hidden four-hour lookahead, visible/hidden state continuity and reporting separation, and reported component totals matching the solved objective. Browser checks cover scenario buttons, Compact/Advanced selection, fixed decimals, hourly aggregation and Excel downloads. Test results establish software behaviour on those inputs, not installed-firmware response or a runtime bound.

### 6.2 Current Implementation

| Component | Implemented behaviour |
| --- | --- |
| Input assembly | `services.planning.plan_as_of` loads cutoff-aware device/state/solar inputs and uses the shared house-load helper for visible plus hidden horizon coverage |
| Scenarios | `planning.scenarios.create_scenarios` creates nine configured low/med/high cases weighted by independent dimension probability products with high-case half-hour timing stress |
| Projection | YAML minute vector with repetition shorthand, intact first action, UTC-clock alignment and full elapsed horizon |
| Solver | One joint-scenario CVXPY/HiGHS mixed-integer problem, three shared mode flags with exactly one active, fresh build/compile, no process pool, compiled cache or warm start |
| Objective | Tariff-scaled, duration-weighted diminishing protection reward minus net cost across visible and hidden periods, with normalised scenario probabilities |
| Bounds | Configured interval-end minimum/maximum are hard constraints; preferred reserve is a soft scoring input |
| Output | `SimulationResult` contains `NextAction`/`OperatingMode` and scenario DataFrames, including post-scaling score contributions; separate lookahead_results retains hidden rows; CLI exports visible reports |
| Dashboard service | `generate_plan_report` accepts an optional browser-local energy-plan overlay and returns simulation/actuals/capacity plus presentation metadata; plans are not persisted, and settings reach disk only through the explicit UI save action |
| Execution | Report-only; no controller, battery commands or automatic replanning |

CLI grouping currently retains only the first score contribution per output bucket rather than summing it; this is a [tracked reporting defect](../reviews/findings.md). Use original simulation periods or Advanced dashboard/Excel for score totals until that path is corrected.

The Plan tab offers a MILP/DP model selector, protection adjustment, a one-line proposed action, expected-cost/range summary, separate Solar/Load Low/Med/High button groups, selected-scenario summary and an actual/forecast chart. Compact detail groups the core columns by local clock hour; crossing periods are split proportionally and battery states interpolated, retaining repeated DST hours. Advanced retains original visible and lookahead periods, selected energy routes, ending SOC, aggregate losses and score contributions. It hides opening battery levels, solar/battery-to-inverter totals and individual conversion losses; the result frames retain them for calculation. Its chart and Excel export also include the lookahead; summary boxes continue to describe the visible day. Headers are not sortable; fixed decimals and Excel exports follow the selected view. See the [dashboard guide](dashboard.md) for exact presentation contracts.

### 6.3 Remaining Limitations and Execution Boundary

The model assumes independent scenario probabilities and full scenario-specific flexibility after the first action. Protection is a preference utility, not a calibrated outage probability. Remaining issues are recorded in [review findings](../reviews/findings.md), including:

- Installed-firmware validation of the declared first-step PV-priority/ceiling response, including subinterval saturation, SOC-stop response and handover.
- A consistent nameplate/health-adjusted SOC convention and mapping metadata cut-offs into physical bounds.
- Site export limits and installed-firmware validation; the collected grid import cap and grid-direction exclusivity are modelled.
- Solar target-time semantics, fallback/padding visibility, forecast input validation and age policy.
- Numerical/approximation tolerances and any deliberate tie-breaking policy; no exact-score secondary solve currently exists.

Before execution, a separate controller needs command ownership, write/read-back verification, stale-plan/failure policy and handover to normal operation. No such controller is part of the current report model.

The valid-input full-range DP search includes a self-consumption continuation and accepts scheduled Min/Max breaches with a finite penalty. An explicitly restricted grid can still exclude all paths; that failure reports a concise warning without a stack trace. The dashboard shows the reason, retains any previous plan and allows retry. Infeasible later refinement stages still retain the earlier feasible result. Unexpected programming/runtime errors retain traceback diagnostics.

DP evaluates the same three policy families as MILP, with positive power choices and explicit zero-power variants for modes 4 and 5. The first action is returned as a Sigenergy mode, charge ceiling, discharge ceiling and optional controller SOC target. See the [DP implementation guide](../implementation/dynamic-programming.md#program-commands-and-modbus-profiles).

### 6.4 Sigenergy policy-to-command mapping

The implemented policy set has **three families** evaluated by DP and MILP. A future controller would execute the returned profile; installed-inverter response has not been verified. The [DP implementation guide](../implementation/dynamic-programming.md#program-commands-and-modbus-profiles) describes the candidate rates.

We arrived at three by grouping desired energy flows by the [remote EMS modes Sigenergy documents](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf), then treating power and SOC targets as parameters of those modes:

1. **Self-consumption** uses mode 2 for ordinary solar and battery use. Sigenergy's [app operating-mode note](https://www.sigenergy.com/en/support/files/1244) describes solar serving the load, then charging the battery, then exporting; the battery serves a solar shortfall.
2. **Force Charge** uses mode 4, which gives PV priority as a charging source. A positive charge ceiling requests charging; a zero charge ceiling with discharge also disabled is the proposed grid-supplied hold. These are settings of one mode, not separate policies.
3. **Force Discharge** uses mode 5, which gives PV output priority over battery output. A positive discharge ceiling requests battery output; a zero discharge ceiling is the proposed PV-first hold/export state. The former **Hold-Battery** policy used the same mode and differs only in rate and when that rate becomes zero, so it does not need a separate policy.

The same two **assumed flow priorities** apply in every policy: **Solar → Load → Battery → Grid**, provided the battery accepts charge in the current mode; and **household load ← PV, then Battery, then Grid**, provided battery discharge is enabled. If a battery direction is unavailable, skip that path in its priority order. Grid is the fallback for household demand, subject to the site's import limit; forced charging may also draw from grid after PV. These are planning assumptions, not a complete flow order documented for Sigenergy's remote modes.

| Policy | Remote EMS mode (`40031`) | Assumed solar priority | Assumed household supply priority | Battery power ceilings | Controller SOC target | Intended use |
| --- | --- | --- | --- | --- | --- | --- |
| **Self-consumption** | **2** — maximum self-consumption | Load → Battery → Grid | PV → Battery → Grid | Normal charge and discharge allowances | None | Store surplus solar and use the battery for household shortfalls. |
| **Force Charge** | **4** — command charging, PV first | Load → Battery → Grid while charging; Load → Grid at a zero charge ceiling | PV → Grid; battery discharge disabled | Requested charge ceiling; discharge ceiling 0 kW. At the target, charge ceiling can become 0 kW. | Optional upper target | Charge during a cheap import period, or hold the battery with both battery directions disabled. |
| **Force Discharge** | **5** — command discharging, PV output first | Load → Battery → Grid when the battery accepts charge; otherwise Load → Grid | PV → Battery → Grid while discharging; PV → Grid at a zero discharge ceiling | Requested discharge ceiling; normal charge allowance. At the target, discharge ceiling can become 0 kW. | Optional lower target | Export valuable battery energy, or prioritise PV output while preventing battery discharge. |

The SOC target is **a controller trigger, not a device register setting**. Leave the installation's Backup SOC (`40046`), charge cut-off (`40047`) and discharge cut-off (`40048`) at their normal values. In particular, `40048` is a general discharge cut-off; it is not documented as the app's per-period “discharge cut-off from BAT to Grid”. The controller monitors measured SOC and, on reaching a target, issues the zero-rate version or the next planned policy. It also issues changed commands at normal plan boundaries. A future controller may refresh its plan at 15-minute boundaries, but must monitor SOC and react within that interval; a 15-minute-only check may overshoot the target. A zero-rate hold must be selected explicitly when the battery should remain above the target: switching to self-consumption may continue discharging to serve the house.

Remote EMS is enabled at `40029`; the ESS charge and discharge ceilings are `40032` and `40034`. The power settings are **ceilings**, not guaranteed achieved rates. Sigenergy's app note supports the self-consumption order; the protocol only documents mode 4's PV-first *charging source* and mode 5's PV-first *output*. DP and the first MILP interval now model the full assumed priorities, including solar serving load before forced battery charging and surplus solar charging before export in mode 5 at zero discharge. Installed-firmware behaviour remains unverified. The [review findings](../reviews/findings.md) track these gaps, including SOC measurement delay, command read-back and loss of telemetry. The application currently reports plans and sends no Modbus control commands.
