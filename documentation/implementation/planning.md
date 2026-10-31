# Planning: database cutoffs, interval objects, solver, and workbook

Sources: [input assembly](../../solar_model/services/planning.py), [as-of queries](../../solar_model/storage/queries/planning.py), [optimisation](../../solar_model/planning/plan_optimisation_milp.py), [physical limits](../../solar_model/planning/system.py) and [reports](../../solar_model/tools/reports.py). This is a report pipeline used by the workbook CLI and dashboard Plan tab with shared first controls and scenario-specific trajectories, without device control.

The scoring implementation follows [plan-model scoring](../overview/plan-model.md#3-scoring): a tariff-scaled protection reward with diminishing returns minus net cost. One joint-scenario solve selects the first mode and shared settings. R and scored battery energy are measured from zero stored kWh, without subtracting a discharge floor.

## 1. CLI call hierarchy

The synthetic performance tool runs with `python -m solar_model.tools.benchmark_planning --output planning-benchmark.json`. Its default nineteen input variants use nine joint scenarios and the historical 43-period grid. `--grid production` selects configured intervals; `--cases` selects named variants and supports `solar_4` and `load_4`. Each case starts in a fresh controller worker and performs one public simulation. No database or device is accessed. JSON records source/configuration hashes, native timings, per-scenario full/visible costs and scores, and battery endpoints.

The controller kills and reaps the simulation process tree after five seconds, records N/A and continues without retrying. Windows uses `taskkill /T`; POSIX isolates the case in a process group. Controller imports/input preparation precede a ready/start handshake with a separate 30-second limit. The five-second budget includes construction, compilation, solving, extraction and physical/cost/duration validation. `simulation_seconds` stops when the public call returns; `total_seconds` includes independent validation. Build, compilation and native solve timings are recorded separately. BLAS/OpenMP limits are one; the single HiGHS solver uses automatic settings. The independently reconstructed weighted report score must match the native objective within 0.000001. See [performance measurements](../reviews/performance.md).

For the alternative DP experiment, add `--model dp --cap-seconds 300`. The default DP deadline is 300 seconds; LP retains five seconds unless explicitly overridden. `--grid quarter-hour` uses exactly 96 fifteen-minute visible intervals starting at the synthetic cutoff, followed by that model's configured lookahead intervals. `--grid production` compares on configured intervals. Preparation/import time remains excluded. The worker validates physical/billing boundaries, scores and per-scenario outcomes for both models; native-objective agreement is checked only for LP because DP reports realised rollout scores rather than a solver objective. JSON includes each first-step outcome and source/configuration hashes. Cases that time out remain N/A and are not retried. DP does not replace the application planner. See the [implementation and limitations](../overview/plan-model.md#25-experimental-dynamic-programming) and [measured comparison](../reviews/planning-dynamic-programming-comparison.md).

```text
solar_model.tools.plan_as_of.main()
  parse args + load_config()
  parse as_of; convert to configured timezone
  SolarDatabase(create_engine_for_database(path))  no schema opener
  delete existing output workbook
  services.planning.plan_as_of()
    load_device_info_as_of()
  load_battery_state_as_of()
  load_forecast_as_of()
    select one forecast vintage
    interval_energies(adjusted) or interval_energies(raw)
    load_house_load_intervals() -> load_hourly_house_load() -> house_load.predict_day()
    construct solar/load ForecastInterval lists
    ForecastData(solar=..., load=...)
  create_scenarios()
    _half_hour_intervals() -> _scenario_intervals()
  simulate_energy_plan()
    validate inputs and build one model with shared mode/command variables
    score and solve all scenarios jointly
    extract selected mode, reconstruct routes and check all balances
    log end and elapsed time
  group_plan(output timestep)
  write_workbook()
```

Database observations and forecast vintages use the cutoff. Efficiencies/tariffs/bounds/weights come from the supplied current configuration; retain that YAML for reproducibility. The script uses the existing schema, writes a workbook, and does not persist plans or send inverter commands.

## 2. DeviceInfo from text change history

`load_device_info_as_of()` inspects whether `variable_name` exists, queries device rows ordered descending ID, and filters parsed UTC timestamps to ≤ cutoff in Python. It keeps the first value for each stable identifier. Missing machine names are derived from human variable text; unit suffixes are appended when needed (`kw`, `kwh`, `kva`, `v`, `hz`, `percent`). Numeric text becomes float; other text stays string.

`config.additional_device_info` then overrides values, providing charge/discharge efficiency. Missing any `DeviceInfo` dataclass field raises an error, even when that field is informational to the solver.

| DeviceInfo fields | Solver use |
| --- | --- |
| `system_battery_capacity_kwh`, `battery_state_of_health_percent` | Usable capacity = product /100 |
| `inverter_battery_charge_power_kw`, `inverter_max_absorption_power_kw` | Separate total battery charging-input limit and grid-to-battery AC absorption limit |
| `inverter_battery_discharge_power_kw`, `inverter_max_active_power_kw` | Discharge energy limit and combined inverter-flow constraint |
| `charge_efficiency`, `discharge_efficiency` | Explicit loss expressions |
| Model/serial/firmware, rated power/apparent power, grid ratings, pack/string/MPPT counts | Required metadata but no direct optimisation constraint |
| Battery discharge SOC cutoff | Does not shift protection scoring; scheduled kWh bounds and action SOC stopping govern the existing flow model |
| Battery charge SOC cutoff and `maximum_pv_input_power_kw` | Required metadata, not newly imposed as hard bounds by the scoring change |

`load_battery_state_as_of()` reads the latest full electrical ORM row with collection epoch ≤ cutoff. It requires SOC and returns `DeviceState(battery_kwh = nameplate_capacity * SOC /100)`. It does not initialise from plant available-discharge energy or apply SOH to this starting value.

## 3. One forecast vintage and its energy dictionary

`load_forecast_as_of()` queries the maximum collection time ≤ cutoff and then all rows for that collection ordered by target. It does not search older complete vintages if that collection is partial.

`interval_energies(rows,panel_ids,zone,source)` extracts non-null cumulative Wh for each configured panel. If any panel has no points, it returns `None`. The caller tries adjusted for all panels, then raw for all panels **in the same collection**; it never mixes sources panel-by-panel.

Each panel's sorted cumulative Wh resets by local target date. First value becomes initial energy; later differences clip decreases to zero; divide by 1000; sum panel contributions into:

```python
dict[datetime, float]  # aware UTC target timestamp -> site interval kWh
```

The planner treats those timestamps as interval **ends**. For retained targets ≥ as-of, first start is target minus `config.forecast.solar.interval_seconds`, then subsequent starts are the preceding retained end. This uses provider collection interval as an initial width; it differs from the dashboard's start-hour interpretation.

UTC dictionary keys preserve distinct autumn fold targets; cumulative resets still use the configured local date. Forecast intervals, scenario generation, projection arithmetic and simulation result timestamps are UTC. The model uses ordinary timedelta arithmetic; tariff/scheduled-limit lookups convert interval starts to configured-local time. Storage-side input assembly retains its local-date handling, and the CLI converts result timestamps for local display.

Solar coverage is clipped/padded to the whole UTC hours surrounding the visible projection plus its 24-hour hidden extension, matching the house-load coverage; leading/trailing missing provider coverage is initially padded with zero solar. The visible part retains that padding; `_lookahead_solar` replaces the hidden day with today's actual-plus-forecast hourly profile. This is a caller policy: `ForecastData` itself rejects extrapolation or gaps.

## 4. House-load forecast inputs

`load_house_load_intervals()` reads hourly `house_load_kwh_period` using `load_hourly_house_load()` and calls the same pure `house_load.predict_day()` helper as daily collection. It uses `forecast.load.model_calibration_parameters` as the single source of load-history tuning. The unused legacy `energy_plan` load-history fields and their validation have been removed. Conversion losses are excluded from forecast demand and applied explicitly by the optimiser.

History ends at the as-of local day's midnight, so neither current-day nor future database observations can leak into the forecast, including forecasts for later projection days. Each target date gets its weekday/weekend profile from this available history. Planning maps the profile to UTC hours, preserving DST, covers whole UTC hours surrounding the projection so the scenario generator can form paired half-hours. The model integrates only the requested projection, prorating its edge intervals. Missing predictions raise a house-load-specific error rather than falling back to the loss-inclusive counter. The numeric `ForecastData.load` field now means household energy demand.

## 5. ForecastData and scenario shapes

```python
ForecastInterval(starts_at=aware_datetime, ends_at=aware_datetime, kwh=0.5)
ForecastData(solar=[...], load=[...])
# Stored attributes after construction:
solar: tuple[ForecastInterval, ...]
load: tuple[ForecastInterval, ...]
```

`ForecastInterval.__post_init__` requires aware timestamps and normalises both endpoints to UTC. `ForecastData` sorts each stream and requires non-empty positive-duration contiguous intervals without overlap. Stored interval comparisons and durations use UTC directly; `sum(start,end)` normalises its query endpoints at entry. `sum(start,end)` returns proportional interval energy, assuming uniform power. It raises if coverage is missing. Repeated local hours remain separate: fold=0 to fold=1 is one elapsed hour.

`create_scenarios(forecast, config.scenarios)` resamples each stream to half-hours, requiring aligned endpoints and whole-hour paired coverage. It creates exactly nine low/med/high combinations using the configured `house_load` and `solar` low/med/high multipliers. Med/med is named `med`; others are named `loadcase_solarcase`. High solar redistributes each paired hour 25%/75%; high load uses 80%/20%. Other cases preserve baseline half-hour timing with the multiplier.

Output contract: `list[ScenarioData]`, where the frozen dataclass has `name: str`, `forecast_data: ForecastData`, and `probability: float`. Names belong to scenarios; `ForecastData` contains only solar/load intervals. Each generated probability is the product of its configured house-load and solar probabilities (independence assumption); these are planning assumptions, not calibrated probabilities or confidence intervals.

`ScenarioDimensionConfig.items()` fixes low/med/high iteration order. `ScenariosConfig` supplies explicit multipliers and probabilities per level; `scenario_probabilities(settings)` computes the nine joint weights for both generation and dashboard averaging. High-case half-hour timing stress remains fixed in the generator. Zero-weight cases remain in results; the optimiser normalises combined probabilities as before.

## 6. Solver hierarchy and numeric inputs

```text
simulate_energy_plan(device_info, state, scenarios, config, projection_start)
  wrapper owns timing/logging and exception reporting
  _simulate_energy_plan:
    validate inputs and create visible/hidden periods
    create system quantities, shared mode flags and command settings
    assemble numeric inputs and common physics
    apply each scenario's three first responses using named masks
    score_simulation supplies the unchanged joint-scenario objective
    compile and solve once with HiGHS
    extract the selected mode, reconstruct reports and validate balances
    return SimulationResult
  log successful end; failures log and re-raise
```

Every call builds and compiles one fresh problem in the calling process. There are no mode workers, process pools, compiled caches, CVXPY input parameters, parameter-update closures or global model locks. Warm starts are disabled and result frames are newly allocated. Input durations can change without cache compatibility checks.

The public wrapper owns elapsed timing and exception reporting. `_simulate_energy_plan` logs projection/complexity and prints solver status, objective and compilation/solve times. Only optimal/optimal-inaccurate statuses are accepted; other statuses raise ValueError. No candidate is silently excluded and no secondary or tie-break objective is added.

The return value is SimulationResult: NextAction contains the solver-selected mode and settings shared across all scenarios; results holds visible frames and lookahead_results retains six hidden rows per scenario. Probabilities must be finite/non-negative with a positive total and are normalised in the score.

`score_scenario(costs, levels, device, config, constraints, durations)` returns total net cost and duration-weighted average battery protection, adding concave piecewise-linear utility bounds to the shared constraints. Costs and levels accept either lists of scalar expressions or a vector expression; the simulation passes scenario columns directly without rebuilding scalar expression lists. Stored energy is scored from zero, without a discharge-floor offset or the extra floor-clamping Boolean variables. `score_simulation(..., projection_starts_at, durations)` calculates `V = X/100 * R * reference_import_rate * projection_hours/24` (using the sum of supplied interval durations for `projection_hours`, or the configured hours when durations are omitted), then returns the weighted average of `-cost + V * protection/100 + terminal_credit`. It does not invoke the solver.

`_reference_import_rate(config, starts_at, ends_at)` samples the import tariff at the start of each elapsed hour in an explicit aware timestamp range. The scoring caller supplies 24 hours, giving exactly 24 values whose arithmetic mean is the reference rate. Each sample uses its configured-local weekday and effective tariff plan; there is no tariff-boundary search or special DST handling. A change between samples is deliberately approximated. A partial final hour in another caller's range is duration-weighted. The mean is clipped to zero if non-positive; missing tariff coverage raises an error. X is `energy_plan.protection_preference` (finite, non-negative, no upper bound; default 50); R is `preferred_reserve_kwh`. The score remains cost-sensitive at X=100. At zero reward, utility variables can remain slack; any reported protection must be recomputed from solved energy.

The four shared first-step variables retain the numeric NextAction fields: charge/discharge power ceilings and SOC stops. A three-element Boolean variable `next_mode` represents self-consumption, charge and discharge, with sum exactly one. Named masks activate the relevant response. Inactive rates are zero; an active forced rate is at least its configured `energy_plan.min_forced_charge_power_kw` or `min_forced_discharge_power_kw` (both default to 0.1 kW). Each active rate is capped by the smaller of its configured `energy_plan.max_forced_charge_power_kw` or `max_forced_discharge_power_kw` (both default to 999 kW) and equipment capability. A minimum above this cap disables that forced mode. SOC settings lie in `[0,100]`. The active charge stop is no higher than the first configured maximum energy converted to SOC; the active discharge stop is no lower than the first configured minimum converted to SOC. Both conversions use the same health-adjusted capacity as `_energy_until_soc`. Inactive stops remain unconstrained by user schedules because they do not control that mode. They are commands, not promises of identical achieved power across scenarios.

`_energy_until_soc` retains exact `min(requested_energy, max(0, SOC_headroom))`, including initial energy beyond the stop and existing efficiency/health-adjusted capacity conventions. Both shared forced requests are constructed; inactive rates imply zero energy. Each scenario computes its own numeric ceilings with ordinary Python min/max. `_minimum` encodes an exact scalar minimum using a bounded binary branch only when the request can cross the ceiling. `_when(mask, value, target, upper, constraints)` means value equals target when the mode mask is one; otherwise it permits their full physical range. A literal product of two unknown solver expressions would be nonlinear, so masking is encoded with bounded linear equalities. No discretionary under-delivery is permitted. Later decisions remain independently optimised per scenario.

- **Self-consumption:** PV serves demand first within AC output capacity. Surplus charges within battery power and physical capacity. Residual demand draws from the battery within available stored energy, discharge power and remaining AC capacity. User schedules do not alter this passive response; the mode is infeasible if that response breaches them. Usable solar is bounded by PV availability and the AC output left after battery charging/discharging. Action SOC settings are ignored as before.
- **Charge, PV first:** achieved charging input is the minimum of the SOC-clipped shared request, battery input limit, physical capacity headroom and `PV + AC absorption allowance`. User maximum energy is enforced through the commanded SOC stop and interval-end constraints, not a separate response clamp. Usable solar is `min(PV, achieved_charge + AC output allowance)`. Grid charging absorbs only the PV shortfall; household demand can draw from the site connection and does not reduce the inverter absorption rating.
- **Discharge, PV first:** usable solar is `min(PV, AC output allowance)`. Achieved discharge is the minimum of the SOC-clipped shared request, battery discharge allowance and AC capacity left after PV. Battery charging is zero. User minimum energy is encoded in the commanded discharge SOC stop. The export cap does not reduce predicted discharge: existing export constraints reject unsafe responses. If battery/inverter output capability exceeds `max(0, demand - usable_solar) + battery_export_allowance`, the shared commanded discharge rate times duration must also fit that allowance. This prevents a high rate followed by an early SOC stop from satisfying only an average-energy cap. The rate bound is conservative even when an already-reached stop prevents all discharge; where inverter headroom itself is within the allowance, no extra rate bound is needed. All nine scenarios constrain the same rate.

These are declared report-model response assumptions: the forced power setting acts as a ceiling and each active mode persists through the interval. They are not installed-firmware verification. SOC stopping and saturation use interval energy under the existing uniform-power approximation; response after a stop, ramping and subinterval PV clipping are not simulated.

`tests/planning/test_next_action.py` checks PV-first discharge saturation, a shared charge ceiling producing different achieved energy across nine scenarios, solar/mixed/grid charging limits, passive response, losses, SOC stopping, rejection of user-bound breaches, command-encoded energy bounds and export-safe shared rates (including early SOC stopping).

The Model to Results section creates a numeric NextAction from the selected mode flag and solved settings. Minimum/maximum report columns retain scheduled bounds. Mode remains explicit even if an already-reached SOC stop makes achieved charging/discharging zero.

`_projection_intervals` reads the initial minute vector from `energy_plan.optimiser_specific.mix_integer_linear_prog.projection_resolution_mins`. The current YAML lists four entries of 15 followed by 23 entries of 60. Cumulative boundaries are anchored at the preceding full UTC hour. The first action uses the first entry intact; later boundaries already overtaken by that action are dropped, and the following interval is shortened to the next configured boundary. The visible endpoint remains `starts_at + num_projection_hours`, with a final partial interval preserving the complete elapsed horizon. A shorter horizon clips the first action if needed. At 10:05 the current vector becomes `[15, 10, 15, 15] + [60] * 23 + [5]` (28 periods).

The grid does not inspect tariffs, battery-limit schedules or midnight. Rates and limits use each interval start; changes inside an interval are approximated. The configured list must be non-empty, contain positive integer minutes and cover at least the configured projection duration. YAML lists become immutable tuples in configuration. Projection arithmetic and output stay UTC, including through repeated local hours.

After `_projection_intervals` produces the visible grid, `_simulate_energy_plan` appends six four-hour slots from its exact endpoint, without tariff or clock alignment. The same state/flow constraints apply to both portions. Logs show visible/hidden counts and the complete minute vector ending in `240*6`. Scoring includes both portions and preserves protection reward per elapsed hour. Extraction validates all periods, then splits the frames so standard CLI exports and dashboard Compact show only the configured visible duration. Dashboard Advanced combines both dictionaries for its chart/table/export; its summary boxes remain visible-only.

Every interval has its own `theta_hrs` and charging/discharging/inverter energy limits. The first-step forced rates and SOC clipping use only the first interval's duration. ForecastData integrates energy over each actual interval. Battery end levels are a `(T,S)` variable matrix with a plain loop adding one vector balance per period, referring only to the previous state; scoring and results reuse these states. This avoids the expanding expression chains found in the earlier implementation.

Forecasts, tariff rates and scheduled bounds use ordinary Python lists indexed `[period][scenario]`. Physical constraints are built one period at a time using scalar limits and one-dimensional scenario lists; costs use one-dimensional lists of rates over time. These pass directly to CVXPY, including `cp.multiply`, without NumPy conversion. CVXPY interprets nested-list constants as columns, so the model deliberately passes only one-dimensional lists. Solver quantities remain six CVXPY matrices: `solar_used`, `battery_charge_input`, `battery_discharge_output`, `grid_import`, `grid_export` and `battery_level_eop`. Boolean battery/grid direction matrices exclude opposed flows. Numeric inputs must be finite; solar, load, initial battery energy and export limits must be non-negative. Neither plan_optimisation_milp.py nor system.py imports NumPy.

All quantities are interval kWh. C is battery charging input before the existing single charging efficiency; D is delivered battery output before accounting for extra depletion. U is usable PV, B stored battery energy and A signed inverter AC output. The physical model is:

```text
0 <= U <= forecast_solar
A = U - C + D
battery_end = battery_start + eta_c * C - D / eta_d
grid_export - grid_import = A - household_demand
-inverter_absorption_kw * dt <= A <= inverter_active_kw * dt
```

Charging input is bounded by `system_limits.max_charge_kwh`, independently of AC absorption. The absorption allowance also respects the inverter active-power bound, as before. Opposed internal AC conversion routes are eliminated: the model represents the single AC port's net direction. Solar can directly charge the battery above the AC rating. Discharge retains the existing minimum of battery discharge and inverter active-power capability. Battery end energy respects configured hard minimum/maximum schedules, with maximum capped by health-adjusted capacity. Missing minimum/maximum schedules still fail tariff-rule lookup.

Grid direction uses `grid_import <= (demand + min(max_charge, max_absorption)) * grid_importing` and `grid_export <= max_output * (1 - grid_importing)`. Bounds scale with each interval duration and are independently applied in each scenario, including hidden slots. They are direction-selection bounds, not new site-meter settings; household demand is not capped by inverter rating. Charging/import and discharging/export can occur in different intervals. Changes within a coarse interval require finer modelling.

The battery-only export limit is retained under the reporting allocation below: `battery_export = min(D, grid_export)`. Therefore `battery_export <= E` is exactly the disjunction `D <= E OR grid_export <= E`. A bounded Boolean branch is added only at cells where E is below maximum discharge; otherwise the cap cannot bind. This permits solar export above the battery export cap, including when that cap is zero. E defaults to 9999 kW times interval duration when its schedule is absent.

`allocate_energy_routes()` in `planning/system.py` accepts matching-length numeric iterables and returns Python lists for the existing report columns. Its loop uses scalar min/max and arithmetic: charging uses PV before grid input; remaining PV supplies household load before battery discharge; each source's remaining output is exported. Solar/grid stored charge is its allocated input times eta_c, with the difference reported as charging loss. Delivered battery output depletes B by D/eta_d. These are deterministic model allocations, not uniquely measured source identities or independent optimisation decisions. They introduce no second gross grid trade. Extraction converts native solver output to lists; scalar min/max also handles numerical clipping and protection contributions before assigning report columns.

`tests/planning/test_grid_exchange.py` checks net-meter billing under export-above-import tariffs, real exports, household demand above inverter rating, different grid directions across scenarios and exclusivity in hidden slots. Extraction checks reconstructed solar/load conservation, battery state and both grid directions against solved values within 0.001 kWh, then calculates costs and unchanged score contributions.

### Design review and retained contracts

| Review concern | Implemented decision |
| --- | --- |
| Preserve one action across nine futures | Three shared mode flags and four scalar command settings in one solve; achieved C/D/U are scenario-specific |
| Preserve economics | Same scenario probabilities, scoring functions, reserve utility/preference, duration weights and terminal credit |
| Preserve equipment/user limits | Explicit battery/DC and inverter/AC boundaries, hard energy schedules, battery export disjunction, direction exclusions and SOC stopping |
| Avoid hidden reporting freedoms | Source routes are reconstructed after solving using one documented allocation |
| Avoid discretionary under-delivery | Exact first-step minima, not upper bounds alone |
| Keep report consumers stable | Existing `SimulationResult`, action fields, route/cost/score columns, visible/lookahead split, charts and exports remain supported |
| Do not imply hardware validation | Ceiling/PV-priority semantics are model assumptions; no command writer, handover or transient device simulation is added |

The objective maximises the probability-weighted average of tariff-scaled, duration-weighted diminishing-return protection reward minus total net cost, as described above. Scenarios share the first-step forced battery rates and SOC cut-offs; their operating mode is selected by the shared Boolean vector. First-step outcomes and all later decisions remain scenario-specific.

## 7. Result DataFrame contract

One frame per named scenario contains `T` ordered rows:

| Columns | Meaning |
| --- | --- |
| `starts_at`, `ends_at` | Aware UTC interval bounds |
| `tariff_name`, `import_rate`, `export_rate` | Interval-start import band and money/kWh rates |
| Minimum/maximum battery kWh, battery export kW limit | Selected enforced schedules |
| `solar_kwh`, `load_kwh` | Input forecast interval energy |
| All route `*_kwh` and three loss fields | Deterministic allocations reconstructed from solved system totals |
| `solar_to_inverter_kwh`, `battery_to_inverter_kwh` | Combined reported route totals |
| `inverter_dc_to_ac_kwh`, `inverter_ac_to_dc_kwh` | Inverter-direction totals |
| `grid_import_kwh`, `grid_export_kwh` | Total grid energy including relevant losses |
| `battery_start_kwh`, `battery_end_kwh`, `battery_charging` | State trajectory and numeric boolean solution values |
| `import_cost`, `export_revenue`, `net_cost` | Energy × applicable rates |
| `cost_score`, `protection_score`, `total_score` | Unweighted scenario contributions; each frame covers its visible or hidden portion |

Solar/load conservation, battery-state changes and both grid directions are checked within 0.001 kWh after reconstructing routes. Only optimal/optimal-inaccurate solver states are accepted; other statuses raise.

## 8. Grouping and Excel output

The CLI converts model timestamps from UTC to `config.timezone` before grouping or workbook export. The default workbook preserves mixed model intervals; grouping runs only when `--output-timestep-min` is explicitly supplied. Grouping combines whole rows by start time and does not split coarse rows into finer estimates. `group_plan()` floors starts to local clock-aligned output buckets, preserving each repeated hour's offset/fold. Energy/cost flows sum; opening/closing battery values take first/last; minimum reserve takes maximum, maximum ceiling takes minimum, export limit takes minimum. Band names join in encounter order. Import/export rates are energy-volume weighted, falling back to the first rate for zero volume. Other untouched columns use the first row.

`write_workbook()` writes Summary plus one sheet per scenario, removing datetime timezone metadata for Excel while retaining local time. Summary contains as-of text, period count and cost/revenue totals. Formatting/tables do not alter calculated results.

No result is written back to SQLite. The existing output path is deleted before loading/solving succeeds, so a failed report can leave no workbook. Relevant current limitations are collected in [review findings](../reviews/findings.md).

## Package API

`planning.types.NextAction` retains charge/discharge power ceilings and SOC stopping settings. `OperatingMode` remains an IntEnum (2/4/5), selected from the solved Boolean mode vector. Positive rates remain consistent with that mode. Timing is implicit and no device commands are executed.

`simulate_energy_plan` accepts device metadata/state, `list[ScenarioData]`, configuration and projection start, and returns `SimulationResult(next_action, results, lookahead_results)`. It has no database/network/UI dependency. The former `build_plan` wrapper is inlined into `services.planning.plan_as_of`, which loads cutoff-aware inputs and generates scenarios directly. The CLI groups and writes `result.results` through `tools.reports`.

`_simulate_energy_plan` reads first-step capacity, charge/discharge limits and loss factors directly from its `SystemLimits` instance. Minimum requested forced powers are planning preferences in `energy_plan.min_forced_charge_power_kw` and `min_forced_discharge_power_kw`, shared by DP and MILP; they are not hardware limits. `system_limits()` derives interval hardware limits; its `max_charge_kwh` and `max_discharge_kwh` variables are energy limits for the interval, not actual or requested charging/discharging. `score_simulation` owns the probability-weighted tariff-scaled reward-minus-cost objective in `plan_optimisation_milp.py`. Production forecast reuse, configured low/med/high scenarios and score diagnostics are implemented. Remaining work includes verified command response, energy/SOC convention reconciliation, input policy and execution; see the [current plan-model reference](../overview/plan-model.md#63-remaining-limitations-and-execution-boundary).

## Experimental DP input preparation

See the [full dynamic-programming walkthrough](dynamic-programming.md) for the backward and forward passes, node definitions, scoring and code flow.

DP defaults to `use_numba: true`; `false` runs the original numerical functions directly in Python. All code lives in `plan_optimisation_dynamic_programming.py`. `compiled_functions()` locally imports Numba and creates cached versions of the same `backward_values()` and `forward_projection()`, registering the existing simulator, interpolation and scoring helpers. Both backends receive the same `DeviceParameters` named tuple, prepared before backend selection; the backward, forward and single-period functions annotate that numerical device type. Named tuples preserve descriptive period/step fields, while typed lists retain the original ragged grids and values for Numba. The same `Period` named tuples serve both paths without a second period type or field-copy conversion. Each includes tariff names and UTC Unix epoch seconds for its start/end; epoch floats preserve fractional seconds. Their lists are converted once per solve pass. Reports convert epochs back to aware UTC datetimes, with no separate metadata collection. There are no separate kernels, packed column arrays or warm-up. Both paths remain independent of MILP and `planning.system`.

Benchmark with `--dp-backend python` or `--dp-backend numba`, which override the configured boolean. Workers start their timers before the first real numerical call, so Numba import/cache loading/compilation is included. `simulation_seconds` includes all stages, preparation and reports; `total_seconds` additionally includes output validation. JSON records the configured `use_numba`, NumPy/Numba versions, `numba_initialisation_in_timing` and the combined planner source hash. The controller startup limit covers input preparation; lazy compilation is subject to the simulation deadline. Install with `python -m pip install -e ".[numba]"`. The earlier [warmed comparison](../reviews/dp-numba-performance.md) uses a different timing scope.

DP runs the configured coarse-to-fine sequence by default: 2% /5 full range, then 1% /10 at +/-5 points, 1% /20 at +/-3 points and 1% /40 at +/-1 point. DP owns its complete duration array, currently 96 quarter-hour visible periods and 24 hourly lookahead periods. These settings live under `energy_plan.optimiser_specific.dynamic_programing`; both optimisers use arrays named `projection_resolution_mins`, accepting positive integer minutes and repetition entries across the full horizon. Explicit `--soc-grid-step-percent` and/or `--power-divisions` select a single DP pass (unspecified values use the configured coarse settings). For example, `python -m solar_model.tools.benchmark_planning --model dp --grid quarter-hour --cases baseline --soc-grid-step-percent 1 --power-divisions 10 --cap-seconds 120` uses a 1% battery grid and ten rates per forced mode, keeping 96 visible quarter-hour periods. These flags do not change MILP calculations.

The DP API also accepts per-period-end `battery_bounds` as SOC fractions: either a common list or a complete scenario-name mapping. Visible-only lists leave lookahead unbounded; full-horizon lists cover lookahead too. These restrict separate value grids and feasible rollout endpoints without changing command SOC stops. `--refine-band-percent 5` runs a 2% /5 coarse solve followed by the requested fine resolution within a scenario-specific five-percentage-point corridor, retaining the better realised policy under one deadline. See the [bounds contract and example](dynamic-programming.md#restricting-the-battery-search-by-period).

`--refinement-stages 1:10:5 1:20:3 1:40:1` runs successive `grid percent:power divisions:half-width percent` stages after the 2% /5 coarse solve. Each corridor is centred on the best retained path so far; failed or lower-scoring attempts leave that path in place. Stage scores, timings and acceptance are recorded, and one case deadline covers the complete sequence. This option cannot be combined with `--refine-band-percent`.

`planning.plan_optimisation_dynamic_programming.simulate_energy_plan` owns the stage sequence and retains the best completed policy. Its `solve_pass()` assembles periods scenario-first. Intervals retain UTC endpoint pairs; the scenario period loop calculates `begin_local` and `end_local` from `begin_utc` and `end_utc`. Tariffs and schedules use `begin_local`; forecasts, elapsed durations and reports use UTC endpoints. For every scenario and UTC interval it resolves the configured-local import/export tariffs, tariff name, minimum/maximum battery schedule, battery-export limit and duration, then integrates the scenario's solar/load forecast and validates inputs. Protection weight is duration-scaled. DP transitions evaluate their local `battery_protection_score()` directly at the ending battery energy, using the exact nonlinear curve instead of `_protection_segments()`. Interpolation is retained only for future-value tables; MILP scoring is unchanged. DP owns local copies of interval/scoring helpers, `SystemLimits`, `system_limits()` and `allocate_energy_routes()` and has no MILP/CVXPY/`planning.system` import dependency. The DP benchmark worker also avoids MILP imports and records the DP source as its scoring/physics implementation. The [lookup-hoisting experiment](../reviews/dp-v1-recheck.md#tariff-hoisting-runtime-comparison) approximately halved preparation time but established no consistent overall runtime gain; that change was subsequently reverted at the user's request.

## Dashboard report entry point

The Plan tab passes a request-local `model` selection (`"MILP"` or `"DP"`) with the protection preference to `plan_with_preference(config, as_of, preference, model="MILP")`. `plan_as_of(database, config, as_of, model="MILP")` assembles the same cutoff-aware device/state/forecast inputs and nine scenarios for either planner, then dispatches to the selected module. DP is imported locally when selected and uses its configured refinement stages/Numba setting; each model keeps its own configured time grid. Unknown model names are rejected before dashboard database creation. Calls that omit the model, including the workbook CLI, continue to use MILP. The choice is not persisted and does not affect other browser clients. The existing startup warm-up still initialises MILP dependencies; DP remains lazy on its first selected run.

`plan_with_preference(config, as_of, preference)` validates a finite non-negative preference without an upper bound, replaces only that setting in a request-local configuration, configures worker logging and opens the existing schema without `open_database()` migration side effects. It calls `plan_as_of` and loads actual telemetry through the same cutoff, returning `PlanReport(simulation, actuals, battery_capacity_kwh)` and closing storage even after failure. The dashboard executes this entry point in a process worker. It neither saves plans/preferences nor changes device settings.

`_forecast_bounds()` returns the UTC floor hour of as-of and ceiling hour of as-of plus horizon. Both solar and house forecasts cover these bounds, supporting arbitrary refresh minutes while preserving whole-hour paired scenario input. Forecast vintage and battery telemetry still use the exact as-of cutoff, and house training still ends at its configured-local midnight. The simulator starts at the exact requested instant and consumes only its full elapsed horizon.

`load_plan_actuals()` selects measured PV power, household load power and battery SOC from configured-local midnight through as-of using ORM-scaled physical units. It converts SOC to stored kWh using device nameplate capacity, then averages onto a UTC minute grid while retaining absent-minute nulls. This differs deliberately from available-discharge energy. It adds no future actuals and no loss-inclusive load counter. Empty telemetry returns an empty frame, allowing a forecast-only chart.

Plan actuals include separate positive import/export kW, splitting the signed grid readings before minute averaging so opposite directions do not cancel. Forecast import/export kW divide the model interval energies by each actual interval duration.

MILP compilation time, native solver time and overall successful simulation time print directly to stdout with immediate flushing, independent of the INFO logging level. DP instead uses the shared `solar_model` logger: a model-start summary, one-based refinement summaries separating compilation from projection time, and one final `DP Project end` total at INFO; detailed per-pass phase timings remain at DEBUG. The total separates compilation from projection time, which includes all stage work and overhead; native-code disk-cache loading is stage work rather than new compilation. See the [DP timing contract](dynamic-programming.md) for details. Start/projection/complexity and exception diagnostics retain application logging. Dashboard workers inherit the launching terminal output (or its configured redirection).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

The report also carries device nameplate battery capacity used for Plan detail SOC (stored kWh / capacity * 100), matching actual-chart and initial-state conventions. This presentation field does not change optimisation or persist results.

Scenario result frames include per-period `cost_score = -net_cost`, `protection_score = V * piecewise_utility(end_kwh)/100 * period_hours/total_hours`, `terminal_score` (final hidden row only), `total_score = cost_score + protection_score + terminal_score`. `_protection_value()` supplies the same tariff/preference scaling V to optimisation and extraction. Extraction evaluates the same `_protection_segments()` envelope used by the LP, using health-adjusted capacity, rather than the exact nonlinear utility used by dashboard summary protection. These contributions are unweighted within each scenario; probability-weighted scenario totals across both visible and hidden frames reproduce the model objective within solver tolerance. Visible report totals alone exclude the lookahead. At zero preference the protection contribution is zero.

### Hidden lookahead and forecast coverage

Forecast queries cover the configured total `num_projection_hours`, rounded out to whole UTC hours for scenario construction. `num_visible_projection_hours` separates reported periods from lookahead; no extra day is appended. Each optimiser's duration array covers the complete horizon. Current MILP lookahead uses six four-hour steps; DP uses 24 one-hour steps. Both integrate forecasts over their exact bounds. Slots use interval-start tariffs/limits without tariff-boundary splits. The visible solar padding and as-of history/vintage policies remain unchanged. Hidden solar instead repeats today's actual-plus-adjusted profile by local clock hour. Four-hour totals hide within-slot solar/load order and allow only one battery direction per slot. `_terminal_rate()` averages 12 hourly import/export midpoints after the full horizon; terminal credit multiplies that rate by final battery kWh and discharge efficiency, independently of preference and without a load cap. See the [complete scoring model](../overview/plan-model.md#hidden-lookahead).

`_lookahead_solar` reads ORM `plant_pv_power_kw` samples from configured-local midnight through as-of, groups them by local clock hour and averages physical kW. For each source hour, past energy is mean actual power times elapsed hours and future energy comes from the selected forecast intervals; an open hour blends both. Missing past samples fall back to forecast with a warning. The existing all-panels raw fallback still applies when adjusted values are unavailable. Each source hour's energy is divided by its elapsed duration; repeated autumn hours are therefore averaged, and a skipped spring hour supplies zero. Hidden intervals use the corresponding local-hour mean power times their elapsed duration, preserving contiguous UTC coverage and the exact visible/hidden boundary. Repetition occurs before scenario generation. No future observations or forecast vintages enter this profile. Sample means assume reasonably regular collection; missing portions inside a sampled hour are not separately gap-filled. This is a planning approximation, not newly persisted forecast history.

The dashboard startup hook submits `warm_planning_worker()` to the same NiceGUI process pool used for plan refreshes. Importing the callback module loads planning dependencies; constructing `highspy.Highs()` initialises the native library without solving. It returns its PID for readiness logging and does not read/write storage or construct/cache a planning problem. Sequential refreshes can reuse that worker; concurrent pool jobs or replacement workers may still incur cold-start overhead.

Startup and plan refresh share `dashboard.planning_worker.ensure_planning_worker()`: one asynchronous warm-up task is created and an early plan request awaits it while showing the normal spinner. Awaiting yields to the main event loop, so other tabs and live updates continue. Cancellation of one waiter does not cancel the shared task. Warm-up failures are logged and release waiting requests to attempt their normal plan run. Concurrent actual plan solves can still require additional pool workers.
