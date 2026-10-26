# Planning: database cutoffs, interval objects, solver, and workbook

Sources: [model](../../solar_model/planning/model.py), [input assembly](../../solar_model/services/planning.py), [as-of queries](../../solar_model/storage/queries/planning.py), [optimisation](../../solar_model/planning/optimisation.py), [physical limits](../../solar_model/planning/system.py) and [reports](../../solar_model/tools/reports.py). This is an offline report pipeline with independent scenario decisions, not device control.

## 1. CLI call hierarchy

```text
solar_model.tools.plan_as_of.main()
  parse args + load_config()
  parse as_of; convert to configured timezone
  SolarDatabase(create_engine_for_database(path))  no schema opener
  delete existing output workbook
  services.planning.plan_as_of()
    PlanningInputs -> planning.model.build_plan()
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
    Build Model section: system_limits(), _energy_until_soc(), inline score expressions
    Optimise Model section: construct problem and solve with HiGHS
    Model to Results section: derive action/mode and extract scenario frames
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
| `inverter_battery_charge_power_kw`, `inverter_max_absorption_power_kw` | Charge energy limit and grid-to-battery absorption constraint |
| `inverter_battery_discharge_power_kw`, `inverter_max_active_power_kw` | Discharge energy limit and combined inverter-flow constraint |
| `charge_efficiency`, `discharge_efficiency` | Explicit loss expressions |
| Model/serial/firmware, rated power/apparent power, grid ratings, pack/string/MPPT counts | Required metadata but no direct optimisation constraint |
| Battery SOC cutoffs and `maximum_pv_input_power_kw` | Required but not enforced; configured kWh bounds govern reserve/ceiling |

`load_battery_state_as_of()` reads the latest full electrical ORM row with collection epoch ≤ cutoff. It requires SOC and returns `DeviceState(battery_kwh = nameplate_capacity * SOC /100)`. It does not initialise from plant available-discharge energy or apply SOH to this starting value.

## 3. One forecast vintage and its energy dictionary

`load_forecast_as_of()` queries the maximum collection time ≤ cutoff and then all rows for that collection ordered by target. It does not search older complete vintages if that collection is partial.

`interval_energies(rows,panel_ids,zone,source)` extracts non-null cumulative Wh for each configured panel. If any panel has no points, it returns `None`. The caller tries adjusted for all panels, then raw for all panels **in the same collection**; it never mixes sources panel-by-panel.

Each panel's sorted cumulative Wh resets by local target date. First value becomes initial energy; later differences clip decreases to zero; divide by 1000; sum panel contributions into:

```python
dict[datetime, float]  # aware UTC target timestamp -> site interval kWh
```

The planner treats those timestamps as interval **ends**. For retained targets ≥ as-of, first start is target minus `config.forecast.interval_seconds`, then subsequent starts are the preceding retained end. This uses provider collection interval as an initial width; it differs from the dashboard's start-hour interpretation.

UTC dictionary keys preserve distinct autumn fold targets; cumulative resets still use the configured local date. Projection/scenario/load steps use `common.add_elapsed`, so each hour has 3600 elapsed seconds across DST. Local labels and tariff lookup remain local.

Leading/trailing missing projection coverage is padded with zero solar. This is a caller policy: `ForecastData` itself rejects extrapolation or gaps.

## 4. House-load forecast inputs

`load_house_load_intervals()` reads hourly `house_load_kwh_period` using `load_hourly_house_load()` and calls the same pure `house_load.predict_day()` helper as daily collection. It uses `forecast.model_calibration_parameters.load`; the older `energy_plan` load-history settings are retained in configuration but no longer control planning. Conversion losses are excluded from forecast demand and applied explicitly by the optimiser.

History ends at the as-of local day's midnight, so neither current-day nor future database observations can leak into the forecast, including forecasts for later projection days. Each target date gets its weekday/weekend profile from this available history. Planning maps the profile to UTC hours, preserving DST, clips intervals to the projection bounds, and prorates partial edge hours. Missing predictions raise a house-load-specific error rather than falling back to the loss-inclusive counter. The numeric `ForecastData.load` field now means household energy demand.

## 5. ForecastData and scenario shapes

```python
ForecastInterval(starts_at=aware_datetime, ends_at=aware_datetime, kwh=0.5)
ForecastData(solar=[...], load=[...])
# Stored attributes after construction:
solar: tuple[ForecastInterval, ...]
load: tuple[ForecastInterval, ...]
```

Construction sorts each stream and requires non-empty positive-duration contiguous intervals without overlap. All comparisons, overlap fractions and durations use UTC instants through `common.utc_instant`; inputs must be aware. `sum(start,end)` returns proportional interval energy, assuming uniform power. It raises if coverage is missing. Repeated local hours remain separate: fold=0 to fold=1 is one elapsed hour.

`create_scenarios()` resamples each stream to half-hours, requiring aligned endpoints and whole-hour paired coverage. It creates nine combinations of load/solar 0.5×, 1×, 1.5×. Base/base is named `base`; others are named `loadcase_solarcase`. High solar redistributes each paired hour 25%/75%; high load uses 80%/20%. Other cases preserve base half-hour timing with the multiplier.

Output contract: `list[ScenarioData]`, where the frozen dataclass has `name: str`, `forecast_data: ForecastData`, and `probability: float`. Names belong to scenarios; `ForecastData` contains only solar/load intervals. All nine generated scenarios initially have probability `1/9`; these are planning assumptions, not calibrated probabilities or confidence intervals.

`LOAD_SCENARIOS` and `SOLAR_SCENARIOS` at the top of `planning/scenarios.py` define each case as `(energy_multiplier, half_hour_split)`. A split of `None` preserves the baseline timing; a pair redistributes each hour's scaled energy between its first and second half-hours. Scenario generation reads both parameters from these definitions.

## 6. Solver hierarchy and arrays

```text
simulate_energy_plan(device_info, state, scenarios, config, projection_start)
  Build Model
    validate names/duration/efficiencies/capacity
    create scenario flow variables and shared first-step rate/SOC variables
    _energy_until_soc -> bounded first-step forced energy
    add scenario constraints and per-scenario score expressions
  Optimise Model
    construct cp.Problem maximising sum(scenario.probability * scenario score)
    solve with HiGHS; reject unsuccessful status
  Model to Results
    derive numeric NextAction and OperatingMode locally
    extract flow/battery values, calculate costs and check energy balances
    return SimulationResult(next_action, results)
```

These stages are inline sections of one function. Shared variables, constraints, score expressions and reporting data remain local; there are no intermediate model objects or separate build/optimise/result APIs. The return value is `SimulationResult`: `next_action` is the shared action including derived mode, and `results` maps scenario names to DataFrames. Each whole-scenario score is multiplied by `ScenarioData.probability` before summation; probabilities are used as supplied, without an additional scenario-count division or normalisation.

The four shared first-step variables match numeric `NextAction`: forced battery charge/discharge rates and their respective SOC cut-offs. Rates are non-negative kW, with at most one positive. Activation Booleans distinguish zero from positive commands using `MIN_FORCE_RATE_KW = 0.1` (100 W); rates remain continuous above this threshold. SOC cut-offs independently lie in `[0, 100]` and need not be ordered.

`_energy_until_soc` implements `min(requested_energy, max(0, SOC_headroom))` using bounded disjunctions. Charge headroom includes charge efficiency; discharge headroom includes discharge efficiency. Reaching the stopping SOC reduces forced energy, and starting at or beyond it produces zero forced energy. With both rates zero, active grid charging and battery export are disabled; passive PV charging and discharge to load ignore action SOC cut-offs. Hardware and scheduled kWh bounds still apply. Later controls remain independent.

The Model to Results section creates a local numeric `NextAction` and derives a local `OperatingMode` IntEnum from the solved rates. The derived mode is stored in `NextAction`, which is returned alongside the scenario dictionary in `SimulationResult`. Minimum/maximum report columns retain scheduled bounds.

Eight non-negative flow variables have shape `(T,S)`: solar→load/battery/grid/clipped, battery→load/grid, grid→load/battery. A boolean `battery_charging` variable has the same shape. Every flow value is interval kWh, not kW.

Loss factors are `1/efficiency - 1`. Solar/grid-to-battery variables represent stored charge; associated loss is additional input energy. Battery-to-load/grid represent delivered AC energy; discharge loss is extra battery depletion.

Key per-interval equations, abbreviated with physical route names:

```text
solar = solar_to_load + solar_to_battery + charge_loss + solar_to_grid + clipped
load  = solar_to_load + battery_to_load + grid_to_load
battery_end = battery_start + stored_charge - delivered_discharge - discharge_loss
grid_import = grid_to_load + grid_to_battery + grid_charge_loss
grid_export = solar_to_grid + battery_to_grid
```

Charge and discharge constraints depend on the boolean mode. Combined DC→AC and AC→DC flows share the maximum active inverter power × `dt`; grid charge also observes absorption power. Battery export observes its scheduled kW limit. End-of-period battery bounds come from configured schedules, with maximum capped by SOH-adjusted capacity. An absent export schedule falls back to 9999 kW; absent minimum/maximum schedules still fail rule lookup.

Objective maximises weighted average negative net cost plus weighted accumulated battery worth across all `(t,s)`. Battery worth is battery end kWh × interval import rate × `dt`. Scenarios share the first-step forced battery rates and SOC cut-offs; their operating mode is derived from those rates. First-step outcomes and all later decisions remain scenario-specific.

## 7. Result DataFrame contract

One frame per named scenario contains `T` ordered rows:

| Columns | Meaning |
| --- | --- |
| `starts_at`, `ends_at` | Aware interval bounds |
| `tariff_name`, `import_rate`, `export_rate` | Interval-start import band and money/kWh rates |
| Minimum/maximum battery kWh, battery export kW limit | Selected enforced schedules |
| `solar_kwh`, `load_kwh` | Input forecast interval energy |
| All route `*_kwh` and three loss fields | Solver values and charge/discharge-loss expressions |
| `solar_to_inverter_kwh`, `battery_to_inverter_kwh` | Combined reported route totals |
| `inverter_dc_to_ac_kwh`, `inverter_ac_to_dc_kwh` | Inverter-direction totals |
| `grid_import_kwh`, `grid_export_kwh` | Total grid energy including relevant losses |
| `battery_start_kwh`, `battery_end_kwh`, `battery_charging` | State trajectory and numeric boolean solution values |
| `import_cost`, `export_revenue`, `net_cost` | Energy × applicable rates |

Solar/load conservation is checked within 0.001 kWh after extracting values. Only optimal/optimal-inaccurate solver states are accepted; other statuses raise.

## 8. Grouping and Excel output

`group_plan()` floors starts to local clock-aligned output buckets, preserving each repeated hour's offset/fold. Energy/cost flows sum; opening/closing battery values take first/last; minimum reserve takes maximum, maximum ceiling takes minimum, export limit takes minimum. Band names join in encounter order. Import/export rates are energy-volume weighted, falling back to the first rate for zero volume. Other untouched columns use the first row.

`write_workbook()` writes Summary plus one sheet per scenario, removing datetime timezone metadata for Excel while retaining local time. Summary contains as-of text, period count and cost/revenue totals. Formatting/tables do not alter calculated results.

No result is written back to SQLite. The existing output path is deleted before loading/solving succeeds, so a failed report can leave no workbook. Relevant current limitations are collected in [review findings](../reviews/findings.md).

## Package API

`planning.types.NextAction` is a frozen dataclass with `force_battery_charge_rate`/`force_battery_discharge_rate` in kW and `charge_cut_off_soc_percent`/`discharge_cut_off_soc_percent`. Timing is implicit. `OperatingMode` remains an `IntEnum` (2/4/5) used for the locally derived mode in `simulate_energy_plan`, but is not an action field or independent solver choice. No device commands are executed.

`PlanningInputs(device_info, device_state, forecast, starts_at)` is a frozen record of the current input objects; contained forecast/device objects are not deeply immutable. `build_plan(inputs, config)` expands the baseline through `create_scenarios` and returns `dict[str, pandas.DataFrame]`. It can run entirely in memory. `services.planning.plan_as_of(database, config, as_of)` supplies the same inputs from cutoff-aware queries. The CLI groups and writes those frames through `tools.reports`.

`system_limits()` converts capacity, health, efficiency and inverter power into interval limits. The inline scenario score is probability times the configured weighted negative net cost plus tariff-valued battery objective. The restructure does not implement the shared action, 0.5/1/2 scenario design or normalised protection scoring in the proposed plan-model document.
