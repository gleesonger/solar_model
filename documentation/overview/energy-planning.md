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

The objective is documented in [plan-model scoring](plan-model.md#3-scoring): retain diminishing-return protection, value it using reserve size, a 0-100 preference and the tariff plan, then subtract electricity cost and maximise the score. Reserve and scored battery energy are measured from zero stored kWh. This objective is implemented; no reference solves or min/max normalisation remain.

Source: [solar_model/planning/optimisation.py](../../solar_model/planning/optimisation.py), `simulate_energy_plan`.

`simulate_energy_plan` wraps the calculation with performance timing, logging and exception reporting. The private `_simulate_energy_plan` builds a fresh CVXPY problem for every call and extracts fresh scenario frames; it directly compiles and solves once with HiGHS, logging projection/complexity and the solver's compilation/solve timings. The calculation's inline sections are Build Model, Optimise Model and Model to Results. It creates shared first-step battery-rate/SOC variables and calls the standalone `score_simulation` for the probability-weighted tariff-scaled reward-minus-cost objective. Forecasts, battery state, timestamps, tariff/limit matrices and the protection value are calculated from the current inputs. There is no compiled-model cache, parameter-update closure, global model lock or intermediate model dataclass; solver warm starts are disabled.

A positive charge rate forces charging until its charge SOC; a positive discharge rate forces discharging until its discharge SOC. Both cannot be positive. With both zero, self-consumption permits passive PV charging and battery discharge to meet demand, ignoring both action SOC cut-offs; existing physical/scheduled energy bounds still apply. Only `t == 0` uses these shared controls. Rates are kW of AC-equivalent battery input or delivered battery output, with a minimum positive command of 0.1 kW (100 W). Internal Boolean constraints handle activation and SOC stopping. After solving, the function derives a local numeric `NextAction` and `OperatingMode` IntEnum; its return value is `SimulationResult(next_action, results)`, combining the shared action (including its derived mode) and scenario-frame dictionary.

- Inputs: `DeviceInfo`, starting battery kWh, `scenarios: list[ScenarioData]`, configuration, projection start. Each scenario contains `name`, `forecast_data` and `probability`; `ForecastData` has no name. The nine generated scenarios have probability `1/9` each.
- `ForecastData` sorts intervals and rejects gaps, overlaps, invalid lengths, and uncovered query ranges. Overlap energy assumes uniform power within each supplied interval.
- Each result is a scenario DataFrame containing interval bounds, tariffs/limits, energy routes/losses, battery levels, and economics. The YAML `projection_resolution_mins` list defines the initial minute vector (shipped as 24 entries of 15 followed by 18 entries of 60). Its first entry is used in full for the next action; subsequent cumulative boundaries align from the preceding full UTC hour, skipping boundaries overtaken by the first action. Hourly periods follow to the complete configured elapsed horizon (24 hours by default), with a final partial period if needed. A 10:05 start produces the benchmark's 43 periods: `[15*1, 10*1, 15*22, 60*18, 5*1]`, ending at 10:05 the next day. A whole-hour start gives 42 periods (`[15*24, 60*18]`); other starting minutes can displace fine blocks. Tariffs, battery-limit schedules and midnight do not add splits; rates and limits use the interval start. Forecast energy and hardware limits use each actual duration; protection is duration-weighted. Battery levels are explicit variables linked by energy-balance equalities, not recursively expanded expressions.
- INFO logs include `plan_model start`, projection period/scenario counts and compact `time_steps=[...]`, scalar variable/Boolean/constraint/parameter counts, solver status/objective and compile/solve timings, then `plan_model end` with elapsed time. Failures log an exception and a failed end status before being re-raised.
- Forecast intervals, scenario timestamps and simulation results are UTC. Plain timedelta arithmetic drives the projection; configured-local conversion is confined to tariff/scheduled-limit lookup. The workbook CLI converts results to local time for display.
- CVXPY solves a **mixed-integer** problem with HiGHS. Boolean charge mode prevents simultaneous battery charging and discharging.
- Flow/state constraints are vectorised across time and scenarios, using numeric forecast/rate/limit matrices. First-action constraints apply to the first row; scoring uses scenario columns. The physical equations and shared-action behaviour are unchanged.
- Energy routes include solar to load/battery/grid/clipping, battery to load/grid, and grid to load/battery.
- Battery capacity is nameplate capacity × state of health; charge/discharge and inverter/absorption powers become interval kWh limits.
- Scheduled battery minimum/maximum apply at interval end; maximum is capped by adjusted capacity. Efficiencies add explicit charge/discharge losses.
- Protection scores stored energy directly from zero. Device discharge SOC does not shift the score; existing physical/scheduled flow bounds remain separate.
- `score_scenario` returns total cost and average diminishing-return protection. `score_simulation` values protection using target reserve, X, the next 24 elapsed hours' average import rate and projection duration, subtracts cost, and averages scenario scores with normalised probabilities. A non-positive average tariff yields zero protection reward.
- Scenarios share first-step control variables; later decisions remain independent. This selects one next action, while the remaining scenario schedules describe possible outcomes.
- Solver infeasibility raises an error; output solar/load balances are checked to 0.001 kWh.

## As-of workbook

Source: [solar_model/tools/plan_as_of.py](../../solar_model/tools/plan_as_of.py), a CLI rather than automated tests.

- Reads the newest device metadata, battery sample, and forecast collection known by the cutoff; it avoids `open_database()` migration side effects.
- The cutoff applies to database observations and forecast vintages. Efficiencies, tariffs, and planning settings come from the supplied configuration; retain that configuration to reproduce a historical run.
- Starting battery uses nameplate capacity × SOC, unlike dashboard available-discharge energy.
- Forecast prefers adjusted cumulative Wh. If any configured panel has no adjusted points, all panels fall back to raw from the same collection; it does not mix sources or search older collections. Missing leading/trailing coverage is padded with zero solar.
- Household demand uses the shared house-load forecaster trained on integrated House Load before the as-of local midnight. It uses the production weekday/weekend shrinkage model and its forecast tuning settings; missing slots raise an error. Conversion losses are applied separately by the optimiser.
- Creates nine low/base/high load×solar combinations, multipliers 0.5/1/1.5. High solar shifts paired half-hours to 25%/75%; high load uses 80%/20%.
- Durations, overlap and projection steps use UTC elapsed time across DST; tariff interpretation remains local and repeated output hours retain their offsets.
- Writes one sheet per scenario and a Summary. Coarser output sums energy/costs, preserves opening/closing battery levels, and combines limits conservatively.
- By default the workbook retains the mixed model intervals. `--output-timestep-min` optionally groups whole rows by their start-time bucket; it does not disaggregate hourly predictions into finer estimates. Pass an explicit timezone offset; off-hour cutoffs are supported. The input loader supplies surrounding full UTC hours for scenario construction; the current solar interval convention differs from dashboard labels.
- The CLI deletes an existing output workbook before solving. Select an expendable output path.

## Independent tariff experiments

- [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) reads a hard-coded database path and emits minute CSV/actuals workbook.
- [check-tariffs/model_tariffs.py](../../check-tariffs/model_tariffs.py) compares enabled JSON tariffs/plans, includes standing charges, and writes Excel summaries. It uses a separate rule-based model and Wh/cents units.
- Both execute top-level work on import; run intentionally from their working folder after inspecting paths. Extraction requires the optional `research` package dependencies (`openpyxl`).

The public calculation entry point is `planning.optimisation.simulate_energy_plan(device_info, device_state, scenarios, config, starts_at)`. It accepts in-memory inputs and returns `SimulationResult`. `services.planning.plan_as_of` loads cutoff-aware inputs and creates scenarios directly; the former `build_plan` wrapper and `PlanningInputs` record have been removed. The CLI passes `SimulationResult.results` to Excel/grouping. `planning/system.py` derives physical limits; the standalone `score_simulation` in `optimisation.py` owns the agreed cost/protection scoring.

The dashboard [Plan tab](dashboard.md#plan-tab) uses the same model and as-of assembly, with a browser-local protection preference, scenario summaries, actual/forecast chart and complete detail table. It runs only on manual refresh in a worker process and does not send device commands.

Model compilation time, native solver time and overall successful simulation time print directly to stdout with immediate flushing, independent of the INFO logging level. Start/projection/complexity and exception diagnostics retain application logging. Dashboard workers inherit the launching terminal output (or its configured redirection).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.
