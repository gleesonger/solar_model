# Economics and energy planning

For as-of queries, device/profile/interval structures, solver variables/equations, and output columns, see [planning implementation](../implementation/planning.md).

## Stored economics

Source: [solar_model/tariffs.py](../../solar_model/tariffs.py), `economic_period_values`.

- Import cost = interval grid-import kWh × import rate.
- Export revenue = interval grid-export kWh × export rate.
- Net cost = import cost − export revenue.
- No-solar/battery comparator = House Load kWh × import rate; it excludes conversion losses and standing charges. Missing House Load produces a missing comparator, with no fallback to the device Load counter.
- Collection stores rates/band/costs at that time. Editing YAML does not retroactively change stored history.
- [solar_model/tools/recalculate_economics.py](../../solar_model/tools/recalculate_economics.py) replaces selected historical values and rebuilds hourly aggregates; see [operations.md](../operations/runbook.md).

## Optimiser contract

The objective is documented in [plan-model scoring](plan-model.md#3-scoring): retain diminishing-return protection, value it using reserve size, a non-negative preference with no upper bound (default 50) and the tariff plan, subtract electricity cost and maximise the score. Reserve and scored battery energy are measured from zero stored kWh. This objective is implemented; no reference solves or min/max normalisation remain.

Source: [solar_model/planning/plan_optimisation_milp.py](../../solar_model/planning/plan_optimisation_milp.py), `simulate_energy_plan`.

`simulate_energy_plan` builds and solves one fresh CVXPY/HiGHS problem across all nine scenarios. Three shared Boolean flags select exactly one first operating mode; power/SOC controls are shared and actual outcomes vary by scenario. Per-scenario first responses use ordinary arithmetic, min/max and named mode masks. Small helpers encode exact solver minimums and conditional equalities; later decisions remain independently optimised. `score_simulation` supplies the unchanged probability-weighted objective. There are no parallel mode workers, persistent pools, compiled caches or warm starts. Public timing includes model construction, compilation, solving and extraction.

The first action is one of three policies: self-consumption, force charge or force discharge, with zero-power variants of the latter two. A positive forced rate is a shared power ceiling constrained by supply, inverter capacity, storage and grid/export limits. Zero charge in mode 4 holds both battery directions; zero discharge in mode 5 prevents battery discharge while allowing surplus PV to charge. The first interval assumes PV serves load before charging and PV output before battery output. The optional SOC target is derived from solved first-interval states for controller monitoring, not written as a device cut-off. Later decisions remain independently optimised.

The solver uses usable PV, battery charging input/delivered output, grid import/export and battery energy. Source-to-destination columns are reconstructed for reporting with solar-to-load priority. `SimulationResult(next_action, results, lookahead_results)` and scoring/report contracts remain supported. The Plan action description shows the selected power ceiling and optional controller target; actual achieved power may be lower.

- Inputs: `DeviceInfo`, starting battery kWh, `scenarios: list[ScenarioData]`, configuration, projection start. Each scenario contains `name`, `forecast_data` and `probability`; `ForecastData` has no name. Each joint probability is the product of its configured solar and house-load probabilities, assuming independence.
- `ForecastData` sorts intervals and rejects gaps, overlaps, invalid lengths, and uncovered query ranges. Overlap energy assumes uniform power within each supplied interval.
- Each result contains interval bounds, tariffs/limits, energy routes/losses, battery levels and economics. Current YAML uses `[15*4, 60*23]`: one hour of quarter-hours followed by 23 hourly steps. Its first step is intact; later boundaries align from the preceding UTC hour and overtaken boundaries are skipped. At 10:05 this gives `[15, 10, 15, 15] + [60]*23 + [5]` (28 periods), preserving the full elapsed horizon. The historical benchmark separately uses a finer 43-period grid. Rates/limits use interval starts, without extra tariff/midnight splits. Forecast energy, hardware limits and protection weights scale with actual duration. Explicit battery states refer directly to the previous period.
- INFO logs identify calculation start, projection intervals, model complexity and failure status. Stdout reports native solver status/objective and compilation/solve timings, then complete public elapsed time. The selected mode is returned in NextAction.
- Forecast intervals, scenario timestamps and simulation results are UTC. Plain timedelta arithmetic drives the projection; configured-local conversion is confined to tariff/scheduled-limit lookup. The workbook CLI converts results to local time for display.
- CVXPY solves a **mixed-integer** problem with HiGHS. Boolean charge mode prevents simultaneous battery charging and discharging.
- A separate Boolean grid direction prevents simultaneous grid import/export in each interval and scenario. The grid import bound includes the collected site import cap and feasible household/charging demand. Grid direction can differ across scenarios or intervals.
- Numeric forecasts/rates/limits use Python lists. Physical equations are added per period, using scalar limits and scenario lists directly with CVXPY; scoring uses scenario columns and one-dimensional rate lists. First-action response constraints apply to row zero. The battery-only export cap constrains the reconstructed battery share independently of PV export.
- Reporting routes include solar to load/battery/grid/clipping, battery to load/grid, and grid to load/battery; their allocations reproduce solved grid totals and stored-energy changes.
- Battery capacity is nameplate capacity × state of health; charge/discharge and inverter/absorption powers become interval kWh limits.
- Scheduled battery minimum/maximum apply at interval end; maximum is capped by adjusted capacity. Four efficiencies add explicit inverter AC-to-DC, inverter DC-to-AC, battery charge and battery discharge losses.
- Protection scores stored energy directly from zero. Device discharge SOC does not shift the score; existing physical/scheduled flow bounds remain separate.
- `score_scenario` returns total cost and average diminishing-return protection. `score_simulation` values protection using target reserve, X, the next 24 elapsed hours' average import rate and projection duration, subtracts cost across the visible horizon and hidden 24-hour extension, and averages scenario scores with normalised probabilities. A non-positive average tariff yields zero protection reward.
- Scenarios share first-step control variables; later decisions remain independent. This selects one next action, while the remaining scenario schedules describe possible outcomes.
- Solver infeasibility raises an error; output solar/load balances are checked to 0.001 kWh.

## As-of workbook

Source: [solar_model/tools/plan_as_of.py](../../solar_model/tools/plan_as_of.py), a CLI rather than automated tests.

- Reads the newest device metadata, battery sample, and forecast collection known by the cutoff; it avoids `open_database()` migration side effects.
- The cutoff applies to database observations and forecast vintages. Efficiencies, tariffs, and planning settings come from the supplied configuration; retain that configuration to reproduce a historical run.
- Starting battery uses nameplate capacity × SOC, unlike dashboard available-discharge energy.
- Forecast prefers adjusted cumulative Wh. If any configured panel has no adjusted points, all panels fall back to raw from the same collection; it does not mix sources or search older collections. Visible leading/trailing gaps are padded with zero solar. The hidden day repeats today's local-hour actual PV profile through as-of plus adjusted forecast for the remainder, with forecast fallback for missing actual hours.
- Household demand uses the shared house-load forecaster trained on integrated House Load before the as-of local midnight. It uses the production weekday/weekend shrinkage model and its forecast tuning settings; missing slots raise an error. Conversion losses are applied separately by the optimiser.
- Creates nine low/med/high load×solar combinations, multipliers/probabilities from `energy_plan.scenarios.house_load` and `energy_plan.scenarios.solar` (shipped 0.5/1/1.5). High solar shifts paired half-hours to 25%/75%; high load uses 80%/20%.
- Durations, overlap and projection steps use UTC elapsed time across DST; tariff interpretation remains local and repeated output hours retain their offsets.
- Writes one sheet per scenario and a Summary. Coarser output sums energy/costs, preserves opening/closing battery levels, and combines limits conservatively.
- By default the workbook retains the mixed model intervals. `--output-timestep-min` optionally groups whole rows by their start-time bucket; it does not disaggregate hourly predictions into finer estimates. Pass an explicit timezone offset; off-hour cutoffs are supported. The input loader supplies surrounding full UTC hours for scenario construction; the current solar interval convention differs from dashboard labels.
- The CLI deletes an existing output workbook before solving. Select an expendable output path.

## Independent tariff experiments

- [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) reads a hard-coded database path and emits minute CSV/actuals workbook.
- [check-tariffs/model_tariffs.py](../../check-tariffs/model_tariffs.py) compares enabled JSON tariffs/plans, includes standing charges, and writes Excel summaries. It uses a separate rule-based model and Wh/cents units.
- Both execute top-level work on import; run intentionally from their working folder after inspecting paths. Extraction requires the optional `research` package dependencies (`openpyxl`).

The public calculation entry point is `planning.plan_optimisation_milp.simulate_energy_plan(device_info, device_state, scenarios, config, starts_at)`. It accepts in-memory inputs and returns `SimulationResult`. `services.planning.plan_as_of` loads cutoff-aware inputs and creates scenarios directly; the former `build_plan` wrapper and `PlanningInputs` record have been removed. The CLI passes `SimulationResult.results` to Excel/grouping. `planning/system.py` derives physical limits; the standalone `score_simulation` in `plan_optimisation_milp.py` owns the agreed cost/protection scoring.

The dashboard [Plan tab](dashboard.md#plan-tab) uses the common as-of assembly with a browser-local MILP/DP selector (MILP initially selected) and protection preference, scenario summaries, actual/forecast chart and complete detail table. It runs only on manual refresh in a worker process and does not send device commands.

Model compilation time, native solver time and overall successful simulation time print directly to stdout with immediate flushing, independent of the INFO logging level. Start/projection/complexity and exception diagnostics retain application logging. Dashboard workers inherit the launching terminal output (or its configured redirection).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

Result frames expose per-period Cost/Protection/Terminal/Total score contributions for Advanced detail and exports. Cost contributes negative net cost; protection contributes the tariff/preference-scaled, duration-weighted LP utility. The total configured horizon includes both visible and lookahead periods; the default split is 24 visible hours within 48 total. MILP uses six four-hour lookahead steps and DP uses 24 hourly steps, both explicitly present in YAML. `SimulationResult.results` holds visible rows; `lookahead_results` holds the extension for diagnostics. Only visible rows feed dashboard summaries, Compact chart/table/downloads and standard CLI exports. Advanced chart/table/downloads also include the lookahead rows. The full probability-weighted objective includes both portions; visible score totals alone do not reproduce it. Terminal credit values final deliverable battery energy at the mean hourly import/export midpoint over the following 12 hours, independently of protection preference; Advanced includes it as Terminal Score. Hidden slots start at the exact visible endpoint, without tariff-boundary alignment; tariffs/limits use the slot start and forecast energies are integrated over its four hours.

An experimental seven-action [dynamic-programming alternative](plan-model.md#25-experimental-dynamic-programming) uses a 5-point SOC value grid and exact physical rollouts. It retains shared-first/scenario-specific-later decisions and the same score. Benchmark with `python -m solar_model.tools.benchmark_planning --model dp --grid quarter-hour --cap-seconds 300`; the LP planner remains the application default. See the [measured comparison](../reviews/planning-dynamic-programming-comparison.md) for accuracy, runtime and approximation limits.
