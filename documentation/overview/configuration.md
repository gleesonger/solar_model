# Configuration

The [runtime implementation guide](../implementation/runtime.md) traces YAML normalisation, dataclass relationships, and which processes/components consume configuration.

Source: [config.yaml](../../config.yaml), [solar_model/config.py](../../solar_model/config.py). Configuration loads at process startup; a managed restart reloads it for all three services. The planning service and dashboard additionally hot-reload the energy_plan section when its startup file changes.

## Explicit configuration is mandatory

Every declared configuration field and containing block must be present in `config.yaml`, even for inactive optimiser models. There are no implicit YAML configuration defaults. Missing values raise a `ValueError` with the full configuration path and stop startup before storage is opened or services launch. The Plan settings editor requires a complete energy-plan section as well.

Nullable values must be written as `null` (including unnamed export/planning rules), and intentionally empty collections as `[]` or `{}`. A nullable type does not make a key optional. Keep the shipped YAML as the complete example when updating a deployed file. New settings must be added explicitly; a newer application must never guess values for an older configuration. Archived energy-plan YAML is displayed unchanged without validation against the current configuration schema. Run Demonstration and Save To Disk validate the editor contents against the current schema and reject missing or obsolete settings; viewing a saved projection does not apply those settings to the running application. Hot reload rejects invalid edits while retaining the previously validated running configuration; it does not apply defaults.

## Sections

| Section | Purpose / shipped values |
| --- | --- |
| `timezone` | `Europe/Dublin`; local tariffs and date boundaries |
| `logging.level` | `INFO`; `DEBUG` adds read-query timing |
| `database.path` | `solar.db`, relative to process working directory |
| `actuals.modbus` | Host/port/timeout/default device ID; shipped port 502, default ID 247 |
| `actuals.data_retrival_schedule` | Live 1 second; full persisted/shared refresh 60 seconds |
| `forecast` | Endpoint, timeout, collection interval 3600 seconds, array definitions and model tuning under `model_calibration_parameters` |
| `dashboard` | Host `0.0.0.0`, port 8090, PV-string-to-panel mapping |
| `tariffs.periods` | Effective-dated import/export schedules |
| `energy_plan` | 24-hour projection, intact 15-minute action, six hours of UTC-clock-aligned fine intervals, then hourly intervals, reserve/protection preference, scheduled battery limits |
| `additional_device_info` | Inverter AC-to-DC 0.970, inverter DC-to-AC 0.974, battery charge 0.970, battery discharge 0.970; modelling assumptions, not measured device efficiencies |
| `energy_plan.scenarios` | Required solar/house-load low, med, high entries, each with multiplier and probability |

These are current file values, not universal defaults. Preserve exact configuration spellings, including `data_retrival_schedule` and `full_updated_interval_seconds`.

## Forecast model calibration parameters

`forecast.solar` groups provider `endpoint`, `timeout_seconds`, collection `interval_seconds`, `arrays`, and `model_calibration_parameters` containing `day_ahead` and `intraday`. `forecast.load.model_calibration_parameters` holds house-load tuning. Each uses `lookback`, `halflife`, and `prior_effective_observations`; see the [forecasting model table](forecasting.md). `lookback`/`halflife` use days for day-ahead/load and hours for intraday. Intraday `lookback: null` preserves all earlier completed hours today. Day-ahead `prior_effective_observations: null` reflects that model's absence of prior shrinkage; a numeric value is rejected rather than ignored.

`prior_effective_observations` is a decimal fraction, not a count: the shipped load setting is `0.2` (20%) and intraday is `0.3333`. The shipped YAML uses `0.3333` for intraday. The derived strength is `1 / prior_effective_observations - 1` (4 for load and approximately 2 for intraday). Values must be finite and in `(0, 1]`; old percentage values such as `20` are rejected. Lookback/halflife values must be finite and positive, except the permitted intraday null lookback. Load lookback and both load/intraday fractions are required. Every solar/load calibration block must explicitly provide all three fields; omitted blocks or values are rejected.

Deployed YAML files must move the former top-level forecast provider/array settings into `forecast.solar`, move `solar_day_ahead`/`solar_intraday` calibration blocks to `forecast.solar.model_calibration_parameters.day_ahead`/`intraday`, and move load calibration to `forecast.load.model_calibration_parameters`. The old flat forecast layout is no longer accepted.

The live collector passes these settings to solar correction and the fixed daily load forecast. `python -m solar_model.tools.evaluate_load --config config.yaml ...` and `python -m solar_model.tools.rebuild_forecasts --config config.yaml ...` load the same settings. Both CLIs use the configured database/timezone unless overridden by `--database`/`--timezone`. The `forecast.load.model_calibration_parameters` tuning block now controls house-load forecasting in both collection and planning. The unused `energy_plan.load_forecast_historical_lookback_window_days` and `load_forecast_historical_lookback_halflife_days` fields have been removed; use this shared forecast tuning for planning too.

## Validation and parsing

`energy_plan.scenarios.solar` and `energy_plan.scenarios.house_load` each require exactly `low`, `med`, `high`, with explicit `multiplier` and `probability` fields. Multipliers must be finite, non-negative and nondecreasing; probabilities must be finite and in [0, 1], totalling 97%-103% per dimension (inclusive); accepted probabilities are normalised to sum to one during loading. Replace former triples/comma-separated settings with this mapping:

```yaml
energy_plan:
  scenarios:
    solar:
      low:  {multiplier: 0.5, probability: 0.2}
      med:  {multiplier: 1.0, probability: 0.6}
      high: {multiplier: 1.5, probability: 0.2}
    house_load:
      low:  {multiplier: 0.5, probability: 0.2}
      med:  {multiplier: 1.0, probability: 0.6}
      high: {multiplier: 1.5, probability: 0.2}
```

Move any former top-level `scenarios` block under `energy_plan`; the old location is rejected. Runtime consumers use `config.energy_plan.scenarios`.

Joint probabilities multiply the two dimension probabilities, assuming independence (Med/Med = 0.36). Zero-probability cases remain available for inspection. Dashboard labels use the configured multipliers and its scenario-average cost uses the joint probabilities.

- YAML is checked recursively for every declared field before dacite deserialisation into frozen dataclasses; lists become tuples. Missing nullable fields are rejected too.
- Unknown keys outside the validated scenario mappings are currently ignored: `dacite.Config` does **not** enable strict mode. This permits descriptive `comment`/period `name` keys but can hide misspellings.
- Planning-rule lists must be supplied explicitly; minimum battery also accepts `{comment: ..., rules: [...]}`. Explicit empty schedules and uncovered intervals retain the documented model semantics below.
- Intervals/timeouts/history lengths must be positive. `energy_plan.num_projection_hours` is the complete solved horizon (48 hours), including lookahead. `num_visible_projection_hours` controls the visible report portion (24 hours). Each optimiser has its own `projection_resolution_mins` array covering the full horizon: MILP `[15*4, 60*23, 240*6]`; DP `[15*96, 60*24]`. Both always use 15 minutes for the first action, regardless of the first configured entry. Subsequent steps that divide an hour align to their UTC clock boundaries and whole-hour multiples align to UTC hours; other step sizes retain their configured duration. At 10:05 the quarter-hour sequence begins `[15, 10, 15, 15]`, and a five-minute tail preserves the elapsed horizon. Both split at the report cutoff and stop at the total horizon; neither appends an implicit extra day. Tariff, battery-limit and midnight boundaries do not add splits.
- Protection preference must be finite and non-negative, with no upper bound; efficiencies must be in `(0, 1]`.
- Validation checks timezone/log level, URL shape, ports/device ID, coordinates/orientation, supported PV strings, and mapped array names.
- Array IDs are unique integers 1–4; names must also be unique and are referenced by `dashboard.actuals_to_forecast` (for example `pv1: East`). Mapping names must match exactly; replace numeric mapping values in deployed YAML files. Empty arrays/mapping and non-positive `peak_kw` are not explicitly rejected.

## Complete values and nullable settings

Use [config.yaml](../../config.yaml) for the complete current field set and selected values. All optimiser blocks, forecast calibration blocks, energy-plan schedules, history settings and time limits are required. Export/planning rule names must be explicit, for example:

```yaml
name: null
```

`forecast.solar.model_calibration_parameters.intraday.lookback: null` and day-ahead `prior_effective_observations: null` are explicit values with documented meanings. Other nullable values must likewise be present. Shipped values are examples, not values to inject if a key is missing. Configuration schema dataclasses have no constructor defaults; recursive validation also prevents nullable types from silently accepting missing keys.

## Tariff and scheduled-limit rules

- Periods must have strictly increasing `effective_from` ISO dates. Lookup chooses the newest period effective on the supplied local date; earlier uncovered dates raise an error.
- YAML `import` becomes dataclass `import_`. Each import band must have a non-empty `name`.
- Rules specify weekday names, `start_time`, `end_time`, and non-negative finite `rate`.
- Start is inclusive; end is exclusive. End may be `24:00`; equal start/end covers a full day.
- A crossing-midnight rule's weekdays identify the starting day. Each tariff direction requires exactly one match per weekday/minute. Battery-limit schedules may have gaps, but overlapping rules are rejected.
- Planning schedules reuse `TariffRule.rate`: it means **kWh** for minimum/maximum battery and **kW** for battery-to-grid limit, not money.
- Where no battery-limit rule matches, the defaults are 0 kWh minimum, 999 kWh maximum, and 999 kW battery-to-grid export limit. The physical battery capacity and equipment limits still apply. Both planning models sample schedules at the interval's local start time.

## Register configuration

- [solar_model/integrations/sigenstor/register_maps/sigen_register_map.json](../../solar_model/integrations/sigenstor/register_maps/sigen_register_map.json) and [solar_model/integrations/sigenstor/register_maps/sigen_device_register_map.json](../../solar_model/integrations/sigenstor/register_maps/sigen_device_register_map.json) are source-relative fixed filenames.
- Explicit map device IDs override the default. Current maps distinguish plant 247 and inverter 1.
- Map `scale` is a **divisor** during decoding, separate from database fixed-point scaling.

## Planning command preferences

`energy_plan.plan_rerun_frequency_mins` is required and must be finite and positive; omission fails startup/configuration-editor validation. The shipped value is 5. The independent planning service checks for newly committed full SigEnergy readings once per second. It starts a background plan when the new reading is at least `max(0, plan_rerun_frequency_mins * 60 - 15)` seconds newer than the reading used by the last successful shared plan. Startup records existing data as a baseline and waits for a newer full reading before the first plan. Changed saved planning settings make the next new reading eligible immediately. Browser-only overrides do not alter this schedule. Save To Disk and external edits hot-reload the energy-plan section, including this interval; other application configuration retains restart semantics. See [Plan tab](dashboard.md#plan-tab).

`energy_plan.min_forced_charge_power_kw` and `energy_plan.min_forced_discharge_power_kw` are finite, strictly positive minimum requested powers in kW, each explicitly set to 0.1 in the shipped YAML. Both DP and MILP use them. They are planning preferences, not verified hardware minimums, and actual flows may be lower when limited by SOC stops or physical capability. They do not scale with interval duration. `energy_plan.max_forced_charge_power_kw` and `max_forced_discharge_power_kw` are finite, strictly positive numeric ceilings, both explicitly set to 999 kW in the shipped YAML; null is not supported. Each effective ceiling is the smaller of the configured maximum and hardware maximum. A minimum above that ceiling makes the forced mode unavailable; self-consumption remains available.

## Planning score

`num_projection_hours` now means the complete horizon, including lookahead. To preserve the former 24-visible-plus-24-hidden arrangement, use `num_projection_hours: 48` and `num_visible_projection_hours: 24`, and include the later durations in each model's array. Replace DP's former scalar `15` with `[15*96, 60*24]` for the current quarter-hour/hourly configuration. MILP uses `[15*4, 60*23, 240*6]`. No implicit 24-hour extension is added.

DP-specific settings live under `energy_plan.optimiser.dynamic_programming`:

```yaml
energy_plan:
  plan_rerun_frequency_mins: 5
  num_projection_hours: 48
  num_visible_projection_hours: 24
  optimiser:
    # model - mix_integer_linear_prog, dynamic_programming
    model: mix_integer_linear_prog
    mix_integer_linear_prog:
      projection_resolution_mins: [15*4, 60*23, 240*6]
    dynamic_programming:
      use_numba: true
      projection_resolution_mins: [15*96, 60*24]
      refinement_stages:
        - {grid_step_percent: 2, power_divisions: 5, band_percent: null}
        - {grid_step_percent: 1, power_divisions: 10, band_percent: 5}
        - {grid_step_percent: 1, power_divisions: 20, band_percent: 3}
        - {grid_step_percent: 1, power_divisions: 40, band_percent: 1}
```

Shared horizon, protection, battery schedules and command power preferences remain directly under `energy_plan`. Move the former flat `projection_resolution_mins` and `dynamic_programming` blocks into the respective sections above; the loader rejects those legacy keys rather than silently ignoring them. All these settings must be explicitly supplied. Both optimisers use a non-empty array of positive integer minute durations, accepting the same repetition syntax. Scalars are rejected. Each array must cover the full `num_projection_hours`; excess entries are clipped at the horizon. `num_visible_projection_hours` (explicitly 24 in the shipped YAML) must be positive and no greater than the total horizon. Power divisions must also be positive integers; grid steps and corridor half-widths must be finite percentages in `(0, 100]`. The non-empty stage list includes the initial full-range solve, whose `band_percent` must be YAML `null` (Python `None`); subsequent stages require numeric half-widths. A list containing only that initial entry requests one full-range solve. The former separate coarse-grid and power settings are rejected; put those values in the first entry instead. Each refinement uses scenario-specific period-end bounds around the best retained path, including all configured lookahead periods. Lower/equal scores and infeasible refinements retain the earlier policy. MILP continues to use `energy_plan.optimiser.mix_integer_linear_prog.projection_resolution_mins`; these DP settings do not change its calculations. See the [DP walkthrough](../implementation/dynamic-programming.md).

`use_numba` is a required boolean, explicitly `true` in the shipped YAML. Set it to `false` for Python execution of the same numerical functions without importing Numba. Numba is included in the normal runtime requirements (the existing `python -m pip install -e ".[numba]"` command is also supported); selecting it without Numba installed raises an installation error. Replace the former `backend` string with this boolean; the old key is rejected. There is no warm-up: the first real numerical call imports Numba and loads or compiles its functions inside the measured solve. This setting affects DP only.

`energy_plan.preferred_reserve_kwh` is a positive, finite target of stored battery kWh measured from zero; it must be supplied explicitly (3 kWh in the shipped YAML). Physical discharge cut-offs do not shift this scoring origin. `energy_plan.protection_preference` is X, any finite non-negative number, required and explicitly set to 50 in the shipped YAML. Values above 100 are supported. Replace the former `optimisation_weights` block with this scalar when updating separate YAML files; omitting the field is rejected. The old battery share corresponds to X = 100 * maximise_battery / (maximise_profit + maximise_battery), but the revised objective changes its effect.

The maximum protection reward is `(X / 100) * reserve_kwh * reference_import_rate * projection_hours / 24`. Here `projection_hours` is the actual simulated duration. The reference is the time-weighted average import rate over the next 24 elapsed hours, clipped to zero if its mean is non-positive. Missing tariff coverage raises an error. The optimiser maximises reward minus net cost, using the existing diminishing-return curve and scenario probabilities normalised to sum to one. X = 0 is cost-only; X = 100 still considers cost. No reference solves or min/max score normalisation remain. Approximation error (0.05 protection points) is a fixed code constant. See [plan-model scoring](plan-model.md#3-scoring).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

`forecast.solar.arrays` has inline comments for each field and links to the [Forecast.Solar estimate API guidance](https://doc.forecast.solar/api:estimate). The official [API specification](https://api.forecast.solar/swagger.yaml) defines the request parameters: geographic coordinates, panel tilt, azimuth relative to south (east -90, south 0, west 90), and installed peak kWp (`peak_kw`). `panel_id` and `name` are local identifiers and are not API parameters.

## UI energy-plan overrides

The Plan settings YAML editor exposes the contents of energy_plan. It accepts a complete replacement, validates using the same normalisation and validation as file loading, and rejects unknown fields. Run Demonstration automatically reruns the plan using an optional per-browser EnergyPlanConfig without modifying global configuration. The schema-aware serializer follows the hand-written YAML layout: blank lines between sections and between the model/mix_integer_linear_prog/dynamic_programming entries within optimiser, inline scenario/refinement mappings, inline weekday/timestep lists and quoted HH:MM times. Consecutive identical timesteps collapse to minutes*count; separated runs stay separate and single steps remain numeric. Scheduled rules with identical start/end times, rate and explicit nullable name share one weekday list in Monday-to-Sunday order. Different names/rates/windows remain separate, including overnight windows; grouping changes representation rather than schedule behaviour. Deserialising the generated YAML restores equivalent settings, and serialising again produces the same text. Descriptive comment fields on scheduled rules are ignored by both file loading and editor parsing. Save To Disk atomically replaces only energy_plan in the startup file, preserving all surrounding text; comments within the section are replaced by the editor text. Saving updates the running background planning settings immediately; a changed energy_plan triggers a shared run after the next new full SigEnergy reading. Other configuration sections still require restart.

Rename former energy_plan.optimiser_specific to energy_plan.optimiser; the old section name is rejected to prevent silently losing settings. optimiser.model must explicitly select mix_integer_linear_prog or dynamic_programming. Generated YAML always includes this field with `# model - mix_integer_linear_prog, dynamic_programming` directly above it. The UI uses the override model; other callers use the global configured model. Save To Disk confirms before persisting, keeps the editor open and does not apply or run the draft.

In DP, `minimum_battery_kwh` and `maximum_battery_kwh` are preferred interval-end levels, with a bounded score penalty for breaches rather than hard SOC stops. The penalty is `-100 * error**2 / (error**2 + 5**2) * period_hours`, where error is the breach in SOC percentage points of health-adjusted capacity. It remains active at zero protection preference and has no additional configuration fields. MILP retains hard scheduled Min/Max limits. Physical capacity and equipment/export restrictions remain enforced in both models.

DP always considers Grid supply and Hold battery alongside self-consumption and forced commands. These passive profiles add no configuration keys and ignore the forced-power preference settings; Grid supply permits charging up to physical battery/equipment capability, while Hold disables both directions.

Solar export first is another always-considered DP candidate with no configuration key or forced rate. Its modelled overflow charging uses physical charge limits and the same soft Min/Max penalty; the mode-5 hardware overflow behaviour remains unverified.

`energy_plan.calculation_timeout_seconds` must be explicitly supplied, finite and positive; the shipped value is 300 (booleans rejected). It bounds each scheduled or private demonstration calculation, including cold worker startup. Deadline failure reaps the worker; a later job creates a replacement. Scheduled timeout detection runs once per second. This is separate from the existing five-second performance-experiment limit.

## Plan snapshot retention

```yaml
energy_plan:
  history:
    retain_snapshots_frequency_mins: 30
    retain_snapshots_period_days: 30
```

Both fields must be explicitly supplied as positive integers; the shipped values are 30. Retain the first successful snapshot per configured UTC slot, with all scenarios and both views, for the configured elapsed days; always protect latest. Cleanup runs only within a successful save. Frequency changes re-bucket surviving snapshots, and cannot restore deleted data. These settings hot-reload with energy_plan and round-trip through the Plan settings editor. See [saved-plan retention](../implementation/plan-history.md#retention-on-successful-submission).
