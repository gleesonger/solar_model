# Economics and energy planning

For as-of queries, device/profile/interval structures, solver variables/equations, and output columns, see [planning implementation](../implementation/planning.md).

The shipped DP configuration uses [Markov uncertainty](../implementation/markov-dynamic-programming.md). Nine changing solar/load layers share a coupled expected-value calculation across the full horizon. Expected cost and score have dedicated result fields. The nine scenario rows are stress illustrations.

## Stored economics

Source: [solar_model/tariffs.py](../../solar_model/tariffs.py), `economic_period_values`.

- Import cost = interval grid-import kWh × import rate.
- Export revenue = interval grid-export kWh × export rate.
- Net cost = import cost − export revenue.
- No-solar/battery comparator = House Load kWh × import rate; it excludes conversion losses and standing charges. Missing House Load produces a missing comparator, with no fallback to the device Load counter.
- Collection stores rates/band/costs at that time. Editing YAML does not retroactively change stored history.
- [solar_model/tools/recalculate_economics.py](../../solar_model/tools/recalculate_economics.py) replaces selected historical values and rebuilds hourly aggregates; see [operations.md](../operations/runbook.md).

## Optimiser contract

Markov DP is the only planner. Services assemble device state, as-of forecasts and nine low/medium/high solar-load combinations; the pure numerical modules have no database, network or UI dependency. One full battery grid and configured power divisions are used for the entire stochastic policy. Model selection, MILP, independent-scenario DP, CVXPY/HiGHS and refinement corridors have been removed.

See the [Markov implementation](../implementation/markov-dynamic-programming.md) and [plan model](plan-model.md) for uncertainty, physical constraints, scoring and expected-versus-stress reporting. All optimiser settings must be explicitly supplied under `energy_plan.optimiser`; no legacy settings are implicitly migrated.

## As-of workbook

Source: [solar_model/tools/plan_as_of.py](../../solar_model/tools/plan_as_of.py), a CLI rather than automated tests.

- Reads the newest device metadata, battery sample, and forecast collection known by the cutoff; it avoids `open_database()` migration side effects.
- The cutoff applies to database observations and forecast vintages. Efficiencies, tariffs, and planning settings come from the supplied configuration; retain that configuration to reproduce a historical run.
- Starting battery uses nameplate capacity × SOC, unlike dashboard available-discharge energy.
- Forecast prefers adjusted cumulative Wh. If any configured panel has no adjusted points, all panels fall back to raw from the same collection; it does not mix sources or search older collections. Visible leading/trailing gaps are padded with zero solar. The hidden day repeats today's local-hour actual PV profile through as-of plus adjusted forecast for the remainder, with forecast fallback for missing actual hours.
- Household demand uses the shared house-load forecaster trained on integrated House Load before the as-of local midnight. Normal and holiday targets share the same three-layer all-days/weekday-weekend/day-of-week model on separate observation streams, with normal-model fallback per hour when holiday history is unavailable. Missing slots raise an error. Conversion losses are applied separately by the optimiser.
- Creates nine low/medium/high load and solar combinations from `energy_plan.scenario_multiplier`. The shipped multipliers are `0.5/1.0/1.5` for solar and `0.7/1.0/1.5` for household load. Each scales the original forecast intervals. Markov transitions supply the outcome probabilities.
- Durations, overlap and projection steps use UTC elapsed time across DST; tariff interpretation remains local and repeated output hours retain their offsets.
- Writes one sheet per scenario and a Summary. Coarser output sums energy/costs, preserves opening/closing battery levels, and combines limits conservatively.
- By default the workbook retains the mixed model intervals. `--output-timestep-min` optionally groups whole rows by their start-time bucket; it does not disaggregate hourly predictions into finer estimates. Pass an explicit timezone offset; off-hour cutoffs are supported. The input loader supplies surrounding full UTC hours for scenario construction; the current solar interval convention differs from dashboard labels.
- The CLI deletes an existing output workbook before solving. Select an expendable output path.

## Independent tariff experiments

- [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) reads a hard-coded database path and emits minute CSV/actuals workbook.
- [check-tariffs/model_tariffs.py](../../check-tariffs/model_tariffs.py) compares enabled JSON tariffs/plans, includes standing charges, and writes Excel summaries. It uses a separate rule-based model and Wh/cents units.
- Both execute top-level work on import; run intentionally from their working folder after inspecting paths. Extraction requires the optional `research` package dependencies (`openpyxl`).

The public calculation entry point is `planning.plan_optimisation_dynamic_programming.simulate_energy_plan(device_info, device_state, scenarios, config, starts_at)`. It accepts in-memory inputs and returns the Markov `SimulationResult`. `services.planning.plan_as_of` assembles cutoff-aware inputs. `planning.system` supplies shared physical limits and energy-route allocation; Markov/DP helpers implement exact nonlinear scoring.

The dashboard [Plan tab](dashboard.md#plan-tab) shows the shared Markov plan and offers private demonstrations. The headless service starts a shared calculation on the first fresh full Sigenergy reading in a later configured UTC slot. The shipped interval is five minutes. Existing startup data establishes the baseline, and saved energy-plan settings apply on the next fresh reading. Browsers following the live plan receive new results through the shared plan store.

The INFO completion log records overall runtime, model compilation, support compilation and projection time. See the [Markov timing fields](../implementation/markov-dynamic-programming.md#python-and-numba).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

Result frames expose per-period Cost/Protection/Terminal/Total score contributions for Advanced detail and exports. Cost contributes negative net cost; protection contributes the tariff/preference-scaled, duration-weighted exact nonlinear utility. The total configured horizon includes both visible and lookahead periods; the default split is 24 visible hours within 48 total. The shipped Markov configuration uses hourly lookahead steps; alignment and the visible cutoff can create partial edge periods. `SimulationResult.results` holds visible rows; `lookahead_results` holds the extension for diagnostics. Only visible rows feed dashboard summaries, Compact chart/table/downloads and standard CLI exports. Advanced chart/table/downloads also include the lookahead rows. The full Markov expected objective includes both portions; visible score totals alone do not reproduce it. Terminal credit values final deliverable battery energy at the mean hourly import/export midpoint over the following 12 hours, independently of protection preference; Advanced includes it as Terminal Score. Tariffs/limits use each slot's start and forecast energies are integrated over its actual duration.
