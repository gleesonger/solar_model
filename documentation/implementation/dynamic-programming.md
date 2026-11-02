# Dynamic programming: how the full calculation works

The experimental v1 planner chooses one battery command to use now, considering all nine solar/load scenarios. The calculation has three stages: build a guide backwards, use it to choose paths forwards, then score those completed paths.

**1. Backward pass:** Build a table of the best estimated future scores from each battery node and time point to the end, separately for each scenario. At each node, try every candidate command and keep the highest estimated score. Each score combines negative net electricity cost (import cost minus export revenue), protection rewards, soft-limit penalties and terminal credit. At the end, we have sampled battery levels at 2% intervals (plus extra exact levels) at every time point in every scenario. Each node has the best estimated score for taking the best available command there and following the best estimated continuation to the end. The table stores that score; the command is chosen again during the forward pass. Reusing these scores speeds up later decisions. A time point is the start or end of a period: for 15-minute periods starting at 08:00, the time points are 08:00, 08:15, 08:30, and so on.

Picture the backward pass as creating a sea of little arrows across time (x-axis) and battery levels (y-axis), guiding travellers towards the overall most optimal solution (highest score). The arrows point towards promising battery levels at the next time point, rather than prescribing commands. The forward traveller tries each available command given their current position (time and battery level), calculates where each would land. They then consult the map to choose the command yieldng the best outcome (highest score) and continuing from there. In the code, this map is stored as scores rather than literal arrows, with interpolation between battery nodes. These scores are the highest possible score that the traveller may obtain from that point forward but sticking to the optimal path.

**2. Forward pass:** Starting from the actual battery level, use the table (aka map) to choose commands. Produce a feasible path with directly calculated flows and battery levels; command selection uses approximate future values.

**3. Final scoring:** Total the path's actual scores and terminal credit, then weight across scenarios. Produce one score per candidate first command, without simulating the path again.

Build the tables once, then repeat stages 2 and 3 for each shared first command. The highest-scoring candidate wins. These are three stages, but only two passes through the table.

This describes the current [implementation](../../solar_model/planning/plan_optimisation_dynamic_programming.py), whose default run starts at a two-percentage-point grid and then refines within scenario-specific corridors. It is an approximate planner, not a guarantee of the globally optimal path. The dashboard Plan tab offers MILP and DP, with MILP selected initially; see the [plan-model reference](../overview/plan-model.md#25-experimental-dynamic-programming).

## Code ownership map

All functions below live in `solar_model/planning/plan_optimisation_dynamic_programming.py`.

| Layer | Records/functions | Responsibility |
| --- | --- | --- |
| Numerical data | `DeviceParameters`, `Period`, `Action`, `Step`, `PlanCommand` | Equipment inputs, scenario-period inputs, candidate commands and calculated period outcomes; shared by Python and Numba |
| Stage orchestration | `simulate_energy_plan`, `result_score` | Run the full-range pass and refinements, retain the best realised probability-weighted full-horizon result, log progress/failure |
| Pass assembly | `solve_pass` | Validate inputs, assemble each scenario's periods/grids, select backend, build backward tables, compare exact forward paths under every shared first command, assemble results |
| Time and value inputs | `projection_intervals`, `protection_value`, `terminal_rate`, `reference_import_rate` | Elapsed UTC intervals, tariff/preference protection scale and terminal battery credit |
| Search space | `actions`, `bounded_grids` | Four passive command profiles, candidate forced rates and per-period-end refinement corridors |
| Numerical search | `backward_values`, `forward_projection`, `interpolate` | Backward future-value tables and exact-state forward policy selection; physical-cap and corridor pruning live in both passes |
| Period calculation | `simulate_system_single_time_period`, `battery_protection_score`, `battery_limit_score`, `step_score` | Physical command response and the shared period objective: economics, protection reward and soft-limit penalty |
| Backend | `get_numba_functions` | Locally import Numba, register shared helpers and lazily compile the same backward/forward functions |
| Report boundary | `result_frame`, `command`, `allocate_energy_routes` | Reconstruct routes, expose score components, split visible/lookahead frames and describe the shared next command |
| Physical inputs | `system_limits`, `SystemLimits` | Health-adjusted capacity, interval equipment/grid-import limits and loss factors |

`services.planning.plan_as_of` calls the public planner. Dashboard column metadata in `dashboard/tabs/plan.py` controls score display, aggregation and Excel export. MILP owns a separate simulator/objective and is unaffected by DP-only changes.

## Pruning

Both backward valuation and forward projection fetch the next grid and value row once per period. Interpolation also keeps its neighbouring coordinates and scores in local variables.

Both passes skip higher rates of the same operating mode once a feasible command has reached a physical cap: those rates cannot improve the capped flow. They also skip higher charge rates after ending above the next search grid, and higher discharge rates after ending below it, because those rates move farther outside the corridor. The threshold command is still scored normally. Separate charge/discharge thresholds reset for every starting battery level; lower rates remain eligible regardless of command order. Infeasible commands do not set a threshold. Candidate order, tie handling, scoring and the shared first command are preserved in Python and Numba.

See the [paired experiment](../reviews/dp-loop-optimisation-experiment.md) for the measured benefit and verification limits.

## Default DP configuration

`energy_plan.optimiser.dynamic_programming` configures the default sequence:

```text
(2% /5, full range)
-> (1% /10, +/-5 percentage points)
-> (1% /20, +/-3 percentage points)
-> (1% /40, +/-1 percentage point)
```

The first `refinement_stages` entry is the full-range solve with `band_percent: null`; its grid and power settings replace the former separate coarse parameters. Later entries use numeric widths. Each corridor is centred on the best retained path so far, separately for every scenario and period end, including lookahead. The first command is still shared across scenarios. If a refinement is infeasible or has a lower/equal realised score, retain the earlier result. Without a known site import cap, the full physical-range pass has a self-consumption continuation: remaining household demand imports from the grid and surplus solar can export or clip. With a known cap, insufficient solar/battery supply can make even the full-range pass infeasible; DP raises `NoFeasiblePlanError` rather than inventing supply or dropping household demand. Scheduled Min/Max breaches carry finite penalties instead of making nodes infeasible. An explicitly restricted single pass can still raise `NoFeasiblePlanError` (a `ValueError` subclass). Timings cover each solve pass, each attempted refinement and the entire plan.

The implementation owns its interval generation, exact protection curve, tariff-reference scaling, terminal credit, physical limits and report energy allocation. All DP code lives in `plan_optimisation_dynamic_programming.py`; DP does not import MILP, CVXPY or `planning.system`. Shared configuration, domain types, tariff lookup and UTC utilities remain common. MILP keeps its own configured timestep grid and calculations, and remains the dashboard/service default.

### Python and Numba backends

`use_numba: true` is the default under `energy_plan.optimiser.dynamic_programming`. Set `use_numba: false` for Python execution. This configuration controls the same original backward and forward functions; there is no separate numerical-kernel implementation or public backend argument. Numba is included in the normal runtime requirements; the existing `python -m pip install -e ".[numba]"` command remains supported. Selecting Numba without installing it raises a clear error. The previous `backend` key is rejected; replace it with this boolean.

The original backward pass, forward pass, single-period simulator and scoring formulas remain in one module. `get_numba_functions()` imports Numba locally and creates cached `njit(cache=True)` versions of `backward_values()` and `forward_projection()`, registering their existing helpers for compilation. An explicit `if use_numba` / `else` selects the functions and inputs for each path. Python calls those same ordinary functions directly. The compiled dispatchers are reused across scenarios, stages and calls. Execution is single-threaded, without relaxed floating-point arithmetic or object-mode fallback. Python execution does not import Numba.

`DeviceParameters`, `Period`, `Action`, `Step`, `PlanCommand` and `SystemLimits` are named tuples with descriptive fields. Python and Numba use the same `Period` records directly, including their tariff names and `starts_at`/`ends_at` as UTC Unix epoch seconds. Epoch values are floats to preserve fractional seconds. Reports convert those two fields back to timezone-aware UTC datetimes using `datetime.fromtimestamp(..., timezone.utc)`. There is no `PeriodInputs` type or separate metadata collection. Both backends receive the same `DeviceParameters` named tuple, constructed once per stage before backend selection. `backward_values()`, `forward_projection()` and `simulate_system_single_time_period()` declare that argument type. Only the Numba path converts the existing lists to `numba.typed.List` once per stage; periods need no field-copy conversion. Ragged grids and value tables retain their original lists-of-rows structure. Completed paths contain the same `Step` named tuples used in Python. Forecast values and list lengths can change without new signatures when element types stay the same.

There is no warm-up or synthetic compilation call. Numba imports, loads its disk cache or compiles lazily on the first real numerical call. The overall DP and benchmark timers include that work on first use. INFO stage summaries separate actual compilation from the remaining projection work using Numba compile-event timing; nested compilation events count only once. Loading native code from disk cache and importing Numba remain projection time, with zero compilation time when no new compilation occurs. Detailed DEBUG phase timers still include compilation within whichever phase triggers it. Timed work includes typed-list preparation, every refinement stage, rollouts and report creation. The few compatibility edits to the original functions are list slicing to reverse the table, an indexed candidate loop, explicit score arithmetic in compiled loops and an equivalent binary search replacing `bisect_left`. Physics and scoring formulas are unchanged.

For equivalent configured runs, compare `--model dp --grid production --cases baseline --dp-backend python` with `--dp-backend numba`; these flags override `use_numba` for that worker. Add `--cap-seconds 120` for a two-minute simulation limit. Each benchmark worker is a fresh process, so its timing includes lazy Numba startup. The earlier [Numba performance measurement](../reviews/dp-numba-performance.md) excluded warm-up and is historical.

The sections below describe one solve pass, which the configured sequence repeats.

## 1. Prepare the time periods and scenarios

Build the complete horizon from `energy_plan.num_projection_hours` (48) and DP's `projection_resolution_mins` array (currently `[15*96, 60*24]`). Shorten only the first configured period whose end passes the next full UTC hour, ending it on that hour, and defer the removed duration to a final period at the projection end. If a period already ends exactly on that first hour, leave the sequence unchanged; later hour crossings are not adjusted. The first command interval can be shortened if it crosses the hour. At 10:05 the first steps are `[15, 15, 15, 10, ...]` minutes and the final step is 5 minutes. At an hour-aligned start, the configured grid still gives 96 quarter-hour visible periods and 24 hourly lookahead periods. Intervals also split at the exact elapsed visible cutoff and clip at the total horizon: at 10:05 this yields 97 visible and 25 hidden periods, with lookahead durations `[55] + [60]*23 + [5]` minutes. The total remains exactly 48 elapsed hours. The array must already cover the complete horizon before alignment; the deferred tail cannot fill an underspecified array. Each actual duration drives power limits, battery flows, protection weights and soft-limit penalties. UTC arithmetic preserves elapsed hours across daylight-saving changes.

For every period in each scenario, determine:

- Solar generation and household demand.
- Import and export prices.
- Soft battery energy preferences and the hard battery-export limit.
- Period duration and protection-scoring weight.

All scenarios use the same time points, but their solar and demand forecasts differ. Intervals contain UTC endpoint pairs. Each iteration of the scenario period loop converts them into `begin_local` and `end_local`. Tariff and schedule lookups use `begin_local`. Periods are assembled separately for each scenario; forecast integration, durations and result timestamps retain UTC intervals.

## 2. Define battery nodes and commands

For every time point in every scenario, create a value-table entry for each battery level: 0%, 2%, 4%, and so on through 100%. A node means one combination of **scenario, time point and battery energy**. With N periods, there are N + 1 time points per scenario, including the start and the final end.

By default, use the same full battery-energy grid across all time points and scenarios. Add the exact current battery energy and all scheduled battery-energy bounds clipped to physical capacity to that grid. These extra entries can sit between the regular 2% levels. Optional search bounds produce separate grids at each period end and scenario. Nodes without a feasible continuation are marked infeasible rather than assigned a finite score.

Define up to fourteen commands:

- Self-consumption.
- Solar charge only: PV supplies household demand first, surplus charges the battery and the grid supplies shortfalls. Battery discharge and grid charging are disabled.
- Solar export first: model PV output to the house/grid up to inverter AC capacity, then charge from otherwise-clipped PV, with no discharge. This mode-5 overflow response is an unverified hardware assumption.
- Hold battery: both battery directions are disabled; PV supplies the house or exports/clips, and the grid supplies shortfalls.
- Charge at five rates: `minimum + (maximum - minimum) * fraction`, for fractions 1/5, 2/5, 3/5, 4/5 and 1.
- Discharge at five rates using the same formula and its own hardware maximum.

A single `solve_pass()` accepts `grid_step_percent` and `power_divisions`, defaulting to 2 and 5. The public `simulate_energy_plan()` runs the configured coarse-to-fine sequence by default. Supplying an explicit resolution or `battery_bounds` to that entry point selects a single pass instead. For a 1% grid with tenths for all forced commands, pass `grid_step_percent=1., power_divisions=10`; this gives 101 regular battery nodes and up to 24 commands. The benchmark accepts the same single-pass overrides as `--soc-grid-step-percent 1 --power-divisions 10`. Without resolution/refinement overrides, `--model dp` uses the configured staged default and its configured grid.

### Restricting the battery search by period

`battery_bounds` accepts one `(lower, upper)` pair per period, in SOC fractions from 0 to 1. Each pair constrains the battery energy at that period's end. The initial energy still comes from the measured device state. These are search bounds; they do not change soft battery preferences or physical SOC stops.

Pass a list to share bounds across scenarios, or a dictionary mapping every scenario name to its own list. Bounds need one pair per actual period returned by projection_intervals, including any alignment tail and visible-cutoff split. For an hour-aligned start, `[(0., 1.)] * 96` covers the visible period ends and 120 pairs cover the whole configured horizon; at 10:05 these counts become 97 and 122. If only visible bounds are supplied, hidden period ends retain full-range bounds. Values must be finite, with `0 <= lower <= upper <= 1`.

At each period end, retain grid nodes inside the bound and add its exact endpoints. The backward pass interpolates against the next period end's own grid. The forward pass rejects commands ending outside that grid, including at the terminal time point. Thus a physically valid command can still be rejected by the search corridor.

The benchmark can run a coarse-to-fine experiment:

```powershell
python -m solar_model.tools.benchmark_planning --model dp --grid quarter-hour --cases baseline --soc-grid-step-percent 0.25 --power-divisions 20 --refine-band-percent 5 --cap-seconds 120 --output dp-refinement.json
```

It first solves at 2% /5, then sets each scenario's bounds to plus/minus five percentage points around its coarse period-end energy, clipped to 0–100%, over both visible and hidden periods. It reruns at the requested finer resolution, re-evaluating all shared first commands. The benchmark retains the coarse result if refinement finds no complete feasible policy or does not improve its realised score. JSON records separate coarse/refinement timings and scores and the selected pass; total simulation time includes both passes and corridor construction. The single deadline covers the complete experiment. Corridors are not widened automatically.

For a sequence of refinements, use `--refinement-stages 1:10:5 1:20:3 1:40:1` instead of `--refine-band-percent`. Each entry is `SOC_GRID_PERCENT:POWER_DIVISIONS:HALF_WIDTH_PERCENT`. The benchmark starts at 2% /5, then centres each stage's bounds on the best retained path from earlier stages. An infeasible or lower-scoring stage is recorded but does not replace that path. JSON includes each stage's attempted score, retained score, duration and acceptance. The deadline covers the coarse pass and all stages together. For a direct comparison, `--refinement-stages 1:40:5` uses just one refinement stage.

The code represents each command as an immutable `Action` named tuple with `mode` and `rate_kw` fields. Actions retain tuple unpacking and comparison, and are constructed once per simulation for reuse in the backward and forward passes.

`energy_plan.min_forced_charge_power_kw` and `energy_plan.min_forced_discharge_power_kw` define separate minimum requested powers, both defaulting to 0.1 kW. These are planning preferences shared by DP and MILP, not verified hardware minimums or minimum achieved flows. Values must be finite and positive, and do not scale with period duration. The power fractions remain defined directly in `actions()`; hardware maximums still come from `SystemLimits`. The numeric `energy_plan.max_forced_charge_power_kw` and `max_forced_discharge_power_kw` settings both default to 999 kW. Both planners cap each configured maximum by hardware capability. DP uses this effective maximum in its five-rate formula.

Omit forced modes whose effective maximum is below the minimum forced power. Remove duplicate choices when the minimum equals the maximum. Each simulated command accounts for efficiencies, inverter limits and battery limits. Its SOC stops use physical 0% and 100%; scheduled Min/Max values affect the score only.

## Program commands and Modbus profiles

`planning.types.PlanCommand` identifies program behaviour independently of `OperatingMode`, whose values remain the Sigenergy remote EMS codes 2/4/5. DP `Action(command_type, rate_kw)` and each simulated `Step.command_type` use this shared integer enum in both Python and Numba. Reports convert the recorded type to its human-readable `plan_action`; they do not infer a command from flow magnitudes. The first command remains shared across all scenarios and later commands are selected independently. Compact hourly reports and grouped CLI reports join distinct labels in encounter order; Advanced and ungrouped reports preserve original periods.

The four passive candidates are evaluated first: Self-consumption, Solar charge only, Solar export first, Hold battery. The passive commands use a zero rate because they have no forced-power request. On exact ties, the earlier candidate wins; Solar charge only therefore wins over Hold when their outcomes are identical, including at night. Solar export first precedes Hold on ties as well; Self-consumption retains priority when it also ties. The passive commands are not subject to forced minimum/maximum requested-power preferences. Physical flow limits and every existing score component still apply. Physical-cap/corridor pruning remains restricted to forced charge/discharge candidates.

`command(action, device)` returns a complete `NextAction` profile. The original forced-rate fields still describe only forced requests. DP additionally fills `command_type`, `charge_power_limit_kw` and `discharge_power_limit_kw` explicitly. Passive allowances use hardware battery power ratings; forced rates are already constrained by equipment and configured forced-power settings.

| Program command | Remote EMS mode (`40031`) | Charge ceiling (`40032`) | Discharge ceiling (`40034`) |
| --- | --- | --- | --- |
| Self-consumption | 2 | Hardware allowance | Hardware allowance |
| Solar charge only | 2 | Hardware allowance | 0 kW |
| Solar export first | 5 | Hardware allowance | 0 kW |
| Hold battery | 2 | 0 kW | 0 kW |
| Force charge | 4 | Selected rate | 0 kW |
| Force discharge | 5 | 0 kW | Selected rate |

This mapping follows the repository's [Sigenergy V2.9 protocol](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf), Table 5-2-1 (printed pages 15-17): both ESS ceilings apply globally regardless of EMS mode and accept zero. Standby mode 1 is not used. The simulator composes these restrictions with its declared PV/household priorities; installed-firmware behaviour remains unverified. In particular, the protocol establishes mode 5 and independent power ceilings but does not establish automatic overflow charging in that mode. `SOLAR_EXPORT_FIRST` is an explicit simulation assumption: charge input is `min(max(0, PV - inverter_AC_capacity), battery_charge_limit, remaining_storage / charge_efficiency)` in interval kWh. PV up to inverter AC capacity supplies the house and then exports; demand beyond that imports from the grid. Charge losses and energy conservation use the existing route allocation. A full/power-limited battery leaves remaining overflow clipped. This responds to inverter capacity only; a separate site export cap is not modelled. Profiles are report data, not executed writes. A future controller must explicitly restore both ceilings when changing profiles; omitted MILP profile fields must not be interpreted as zero.

## Soft battery limits and period scoring

DP treats `minimum_battery_kwh` and `maximum_battery_kwh` as preferred interval-end levels. They neither clamp charge/discharge nor reject a response. Their original configured values remain in report rows, including preferences outside physical capacity; only grid nodes are clipped to the physical range. The schedules must remain finite and ordered, with a non-negative maximum. A negative minimum is ineffective within the physical energy range. Physical battery capacity (nameplate times health), equipment power/efficiency constraints and battery-export restrictions remain enforced. MILP retains its existing hard scheduled limits.

`battery_limit_score()` computes a bounded penalty independently of protection preference:

```text
breach_kwh = max(minimum - ending_energy, ending_energy - maximum, 0)
error = 100 * breach_kwh / health_adjusted_capacity
limit_score = -100 * error**2 / (error**2 + 5**2) * period_hours
```

For a positive breach, the implementation uses the reciprocal ratio `ratio = capacity * 0.05 / breach_kwh` and `-100 / (1 + ratio * ratio) * period_hours`, avoiding squaring a potentially large breach. Zero breach returns zero directly. There is no discontinuity at the boundary. A one-percentage-point breach scores approximately -3.85 per hour; five points score -50 and ten points score -80 per hour. The penalty accelerates initially, then gradually saturates. The penalty approaches -100 per hour. It uses the ending state, rather than an integral of the within-period breach.

`Step` carries `limit_score` alongside `net_cost`, `protection_score` and the selected `command_type`. `step_score()` is the single period objective used by both numerical passes and realised path scoring via `Step.score`:

```text
period_score = -net_cost + protection_score + limit_score
```

`result_frame()` exposes each component and adds final-period terminal credit to `total_score`. Refinement acceptance compares those same realised total scores. The next command uses physical 100% charge/0% discharge stops; a preferred boundary is never restored as a hard command stop. The physical-cap and corridor pruning rules remain valid because larger commands either reproduce the same complete step or move farther outside the search corridor.

## 3. Value every node, working backwards

DP calculates each period's protection reward with the exact nonlinear formula, rather than MILP's straight-line approximation. For ending battery energy E and preferred reserve R, the raw protection score is `100 * (1 - 0.3 ** (E / R))` when `E <= R`, otherwise `70 + 30 * (E - R) / (E + R)`. Multiply by the period's protection weight for its reward. Future-value interpolation between battery nodes remains a separate approximation.

For each scenario, calculate the best estimated remaining score at every node. This answers: "If the battery has this much energy at this time, how valuable is the best continuation?"

The final time point is a special case: there are no periods left to simulate, so each node receives only terminal credit:

```text
terminal value = battery energy × terminal credit rate
```

Next, move back one period. Call its starting time point t−1 and its ending time point t. The values at t are already available.

For every battery node at t−1:

1. Assume the battery starts at that level.
2. Try every command and calculate the flows and ending battery level at t.
3. Discard infeasible commands.
4. Calculate each remaining command's estimated score:

   ```text
   − net cost during the period
   + protection reward for the ending battery level
   + soft-limit score for the ending battery level
   + interpolated future value at that ending level
   ```

5. Store the highest score for that starting node at t−1. The code stores the score, rather than the winning command.

For example, a command starting at 40% might end at 43%. Estimate its future value from the stored values at 42% and 44% at t. The battery itself remains at 43%. If either required neighbouring value is infeasible, the interpolated continuation is rejected.

Continue one period backwards at a time. **Stop after calculating the nodes at the start of the first period—the projection start.** At that point, every time point has been processed for that scenario. Repeat for all scenarios.

Working backwards describes the order in which the table is filled. Every command simulation still moves forwards from its assumed starting energy to its ending energy.

## 4. Understand what the backward pass provides

We now have a future-value table for every scenario, covering all battery nodes and time points. These tables guide us towards the best estimated continuation from each starting state.

You can picture a path as a line moving through time across the battery levels, always choosing the command that looks best according to the table. There are different best continuations from different starting states. Actual transitions can land between nodes, so these lines do not have to connect exact 2% nodes.

The code has not yet selected or stored one complete optimal path. It has stored values, and the forward pass will use them to choose the path. Because the grid and command choices are approximate, that path is not guaranteed to be globally optimal.

A small change in the initial battery level can move us to a different starting point and a different preferred path. Small changes in prices, forecasts or other inputs can also switch which command looks best, even from the same starting point. For changed inputs, the value tables must be recalculated.

## 5. Try a shared first command

Start from the actual current battery energy.

Choose one candidate first command—for example, charging at the lowest candidate rate—and require every scenario to use it in the first period.

The command is shared, but its resulting flows and ending battery energy can differ between scenarios because solar and demand differ.

## 6. Simulate each scenario forwards

For each scenario:

1. Apply the fixed first command and calculate its actual ending battery energy.
2. Carry that energy into the next period.
3. From the second period onwards, try every command from the actual battery level.
4. Score each feasible command using:

   ```text
   score for this period
   + interpolated future value at its ending battery level
   ```

   This is an **approximate score for choosing the command**, because the future part comes from the interpolated table. In the next section, we calculate the realised score of the completed path directly, without interpolating its future value.

5. Select the best command and carry its actual ending energy forwards.
6. Continue until the final time point.

Battery energy is never rounded to a 2% node. Only future value is interpolated. If any scenario cannot produce a complete feasible path, reject that first-command candidate.

## 7. Calculate the realised score of each completed path

Calculate the score directly from the flows and battery levels actually simulated:

```text
scenario score =
    sum of period protection rewards
    − total net cost
    + sum of period soft-limit scores
    + terminal credit
```

This is the exact score of that simulated path under the model's scoring rules, using the exact nonlinear protection formula. It is not a claim that the path is optimal or that forecasts exactly predict reality.

Then combine the scenario scores:

```text
combined score = sum of (scenario probability × scenario score)
```

Interpolation guided the forward choices; this final comparison uses realised path scores. Calculating these scores does not change the commands already selected during the rollout.

## 8. Repeat for every first command

Repeat the forward calculation with each possible first command fixed across all scenarios. Reuse the same backward value tables.

Select the first command with the highest combined realised score among candidates with complete feasible paths in every scenario.

Later commands can differ between scenarios; only the first command must be shared.

## 9. Return the command and reports

Return:

- The selected first command, including its program command, Sigenergy mode, both global power ceilings and SOC stops.
- The simulated visible-day results for every scenario.
- The additional lookahead results.

The full-range pass includes self-consumption and can return a plan despite unavoidable scheduled Min/Max breaches. Explicit search corridors can exclude all paths and raise `NoFeasiblePlanError`; an unsuccessful refinement keeps the earlier feasible plan. Invalid inputs and unexpected runtime errors still fail explicitly.

## Code flow

The source is ordered top down: data types first, then `simulate_energy_plan()`, followed by helpers with callers above the functions they call.

`interpolate(x_values, y_values, x)` estimates the future score (`y`) at an ending battery energy (`x`).

DP function names have no leading underscores. Its interval/scoring helpers are `projection_intervals()`, `protection_value()`, `terminal_rate()` and `reference_import_rate()`; MILP retains its own function names.

`simulate_energy_plan()` emits these INFO milestones through the shared `solar_model` logger. All DP timing fields (including DEBUG and failure logs) show seconds to three decimal places (1 ms); score precision is unchanged:

- `DP Model Start`: total projection hours, actual number of time steps, number of stages and `use_numba`.
- `DP Refinement Stage #1 complete` (and subsequent one-based stages): `compilation_seconds`, `projection_seconds`, retained `score` and `accepted` (true when the stage improves the retained plan). Projection time includes all stage work apart from compilation: preparation, backward value tables, forward rollouts, scoring and reports. Initial model setup is included in stage 1. Compilation is observed during real calls; there is no warm-up.
- Exactly one `DP Project end`: total `elapsed_seconds`, summed `compile_seconds`, `projection_seconds` (all non-compilation time, including stage work and logging between stages) and final score. Compilation + projection equals the total, apart from rounding.

Single-pass overrides also produce these milestones with one stage. An infeasible later stage logs a warning and retains the earlier result; an infeasible explicitly restricted pass logs a concise warning without a traceback and raises `NoFeasiblePlanError`. Unexpected errors log `DP Project failed` with elapsed time and traceback, then raise without a success summary. Each internal `solve_pass()` logs only its detailed `dp_model timings:` phase breakdown at DEBUG. These inclusive phase times cover validation, setup, period assembly, grids/actions, backward values, forward rollouts, scoring and reports. Forward time excludes separately accumulated path scoring. Timings aggregate all scenarios and candidate commands; no timers are placed inside individual transition calculations. Total time ends before emitting its log message.

```text
solve_pass(...)
├── Validate inputs
├── Prepare time intervals, scoring and scenario periods
├── Build battery grid
├── actions(...) → candidate commands
├── For each scenario: backward_values(...)
│   └── For each period backwards, battery node and command:
│       ├── simulate_system_single_time_period(...) → flows and period score, or infeasible
│       └── interpolate(...) → estimated future value
├── For each candidate shared first command:
│   ├── For each scenario: forward_projection(...)
│   │   └── For each period forwards:
│   │       ├── simulate_system_single_time_period(...) → actual next battery energy
│   │       └── interpolate(...) → guide command selection
│   └── Calculate combined realised path score
├── Select best shared first command
├── result_frame(...) → per-scenario selected command labels, energy routes and reports
└── Return SimulationResult with command(...), results and lookahead_results
```


## Grid connection import limit

`DeviceInfo.grid_max_import_power_kw` carries the latest as-of device-history reading of V2.9 holding register 40040. Positive infinity means no reading is known; zero means no grid imports are allowed. Negative and NaN limits are rejected. Both system-limit helpers expose `max_grid_import_kwh = grid_max_import_power_kw * interval_hours`; DP's numerical `DeviceParameters` carries the kW value for the shared Python/Numba period calculation.

Every command and scenario, including hidden lookahead periods, must satisfy `grid_import <= grid_max_import_power_kw * hours` (1e-9 kWh numerical tolerance). Forced charging also caps its charging input at `max(0, solar_kwh + import_limit_kwh - load_kwh)`, alongside battery, SOC and inverter limits. This reserves import capacity for household demand and permits solar charging beyond the site import cap. This saturation is a model assumption about the configured global limit, not verified firmware behaviour. The final import check rejects any command whose remaining household demand exceeds the cap, including passive modes and empty-battery states. No load shedding is modelled. Infeasible candidate transitions are excluded from backward values and forward rollouts; if no complete shared-command plan exists, `NoFeasiblePlanError` is reported. This is an interval-average power constraint; transient peaks are not modelled.

The setting appears automatically in the System information table through device history. Collection and modelling send no control writes. MILP currently carries the field through its inputs/system-limit result but does not enforce it in its optimisation constraints.
