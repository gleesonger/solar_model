# Configuration

The [runtime implementation guide](../implementation/runtime.md) traces YAML normalisation, dataclass relationships, and which processes/components consume configuration.

Source: [config.yaml](../../config.yaml), [solar_model/config.py](../../solar_model/config.py). Configuration loads at process startup; a managed restart reloads it for both children.

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
| `additional_device_info` | Charge/discharge efficiencies, both shipped as 0.95 |
| `energy_plan.scenarios` | Required solar/house-load low, med, high entries, each with multiplier and probability |

These are current file values, not universal defaults. Preserve exact configuration spellings, including `data_retrival_schedule` and `full_updated_interval_seconds`.

## Forecast model calibration parameters

`forecast.solar` groups provider `endpoint`, `timeout_seconds`, collection `interval_seconds`, `arrays`, and `model_calibration_parameters` containing `day_ahead` and `intraday`. `forecast.load.model_calibration_parameters` holds house-load tuning. Each uses `lookback`, `halflife`, and `prior_effective_observations`; see the [forecasting defaults table](forecasting.md). `lookback`/`halflife` use days for day-ahead/load and hours for intraday. Intraday `lookback: null` preserves all earlier completed hours today. Day-ahead `prior_effective_observations: null` reflects that model's absence of prior shrinkage; a numeric value is rejected rather than ignored.

`prior_effective_observations` is a decimal fraction, not a count: load defaults to `0.2` (20%) and intraday to one-third. The shipped YAML uses `0.3333` for intraday. The derived strength is `1 / prior_effective_observations - 1` (4 for load and approximately 2 for intraday). Values must be finite and in `(0, 1]`; old percentage values such as `20` are rejected. Lookback/halflife values must be finite and positive, except the permitted intraday null lookback. Load lookback and both load/intraday fractions are required. Within the new solar/load sections, omitted calibration blocks inherit defaults; a supplied model block must provide its three fields.

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

- YAML is deserialised through dacite into frozen dataclasses; lists become tuples. Required dataclass fields must be present.
- Unknown keys outside the validated scenario mappings are currently ignored: `dacite.Config` does **not** enable strict mode. This permits descriptive `comment`/period `name` keys but can hide misspellings.
- Optional planning-rule tuples default empty; minimum battery also accepts `{comment: ..., rules: [...]}`. The optimiser currently still requires usable minimum/maximum rules.
- Intervals/timeouts/history lengths must be positive. `energy_plan.num_projection_hours` is the complete solved horizon (48 hours), including lookahead. `num_visible_projection_hours` controls the visible report portion (24 hours). Each optimiser has its own `projection_resolution_mins` array covering the full horizon: MILP `[15*4, 60*23, 240*6]`; DP `[15*96, 60*24]`. MILP preserves its intact first action and UTC-aligned visible boundaries, then starts configured lookahead durations at the exact visible endpoint. DP shortens only the first period crossing the next full UTC hour and moves the removed duration to the projection end. An exact hour endpoint needs no adjustment; later crossings are unchanged. The first command period can be shortened. At 10:05 the quarter-hour sequence begins [15, 15, 15, 10] minutes and ends with an extra five-minute interval, preserving the total elapsed horizon. Both split at the report cutoff and stop at the total horizon; neither appends an implicit extra day. Tariff, battery-limit and midnight boundaries do not add splits.
- Protection preference must be finite and non-negative, with no upper bound; efficiencies must be in `(0, 1]`.
- Validation checks timezone/log level, URL shape, ports/device ID, coordinates/orientation, supported PV strings, and mapped array names.
- Array IDs are unique integers 1–4; names must also be unique and are referenced by `dashboard.actuals_to_forecast` (for example `pv1: East`). Mapping names must match exactly; replace numeric mapping values in deployed YAML files. Empty arrays/mapping and non-positive `peak_kw` are not explicitly rejected.

## Tariff and scheduled-limit rules

- Periods must have strictly increasing `effective_from` ISO dates. Lookup chooses the newest period effective on the supplied local date; earlier uncovered dates raise an error.
- YAML `import` becomes dataclass `import_`. Each import band must have a non-empty `name`.
- Rules specify weekday names, `start_time`, `end_time`, and non-negative finite `rate`.
- Start is inclusive; end is exclusive. End may be `24:00`; equal start/end covers a full day.
- A crossing-midnight rule's weekdays identify the starting day. Validation requires exactly one match per weekday/minute for each tariff direction and each non-empty planning schedule.
- Planning schedules reuse `TariffRule.rate`: it means **kWh** for minimum/maximum battery and **kW** for battery-to-grid limit, not money.

## Register configuration

- [solar_model/integrations/sigenstor/register_maps/sigen_register_map.json](../../solar_model/integrations/sigenstor/register_maps/sigen_register_map.json) and [solar_model/integrations/sigenstor/register_maps/sigen_device_register_map.json](../../solar_model/integrations/sigenstor/register_maps/sigen_device_register_map.json) are source-relative fixed filenames.
- Explicit map device IDs override the default. Current maps distinguish plant 247 and inverter 1.
- Map `scale` is a **divisor** during decoding, separate from database fixed-point scaling.

## Planning command preferences

`energy_plan.min_forced_charge_power_kw` and `energy_plan.min_forced_discharge_power_kw` are finite, strictly positive minimum requested powers in kW, each defaulting to 0.1. Both DP and MILP use them. They are planning preferences, not verified hardware minimums, and actual flows may be lower when limited by SOC stops or physical capability. They do not scale with interval duration. `energy_plan.max_forced_charge_power_kw` and `max_forced_discharge_power_kw` are finite, strictly positive numeric ceilings, both defaulting to 999 kW and explicitly set in the shipped YAML; null is not supported. Each effective ceiling is the smaller of the configured maximum and hardware maximum. A minimum above that ceiling makes the forced mode unavailable; self-consumption remains available.

## Planning score

`num_projection_hours` now means the complete horizon, including lookahead. To preserve the former 24-visible-plus-24-hidden arrangement, use `num_projection_hours: 48` and `num_visible_projection_hours: 24`, and include the later durations in each model's array. Replace DP's former scalar `15` with `[15*96, 60*24]` for the current quarter-hour/hourly configuration. MILP uses `[15*4, 60*23, 240*6]`. No implicit 24-hour extension is added.

DP-specific settings live under `energy_plan.optimiser.dynamic_programming`:

```yaml
energy_plan:
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

Shared horizon, protection, battery schedules and command power preferences remain directly under `energy_plan`. Move the former flat `projection_resolution_mins` and `dynamic_programming` blocks into the respective sections above; the loader rejects those legacy keys rather than silently ignoring them. These are also the defaults when omitted. Both optimisers use a non-empty array of positive integer minute durations, accepting the same repetition syntax. Scalars are rejected. Each array must cover the full `num_projection_hours`; excess entries are clipped at the horizon. `num_visible_projection_hours` (default 24) must be positive and no greater than the total horizon. Power divisions must also be positive integers; grid steps and corridor half-widths must be finite percentages in `(0, 100]`. The non-empty stage list includes the initial full-range solve, whose `band_percent` must be YAML `null` (Python `None`); subsequent stages require numeric half-widths. A list containing only that initial entry requests one full-range solve. The former separate coarse-grid and power settings are rejected; put those values in the first entry instead. Each refinement uses scenario-specific period-end bounds around the best retained path, including all configured lookahead periods. Lower/equal scores and infeasible refinements retain the earlier policy. MILP continues to use `energy_plan.optimiser.mix_integer_linear_prog.projection_resolution_mins`; these DP settings do not change its calculations. See the [DP walkthrough](../implementation/dynamic-programming.md).

`use_numba` is a boolean, defaulting to `true`. Set it to `false` for Python execution of the same numerical functions without importing Numba. Numba is included in the normal runtime requirements (the existing `python -m pip install -e ".[numba]"` command is also supported); selecting it without Numba installed raises an installation error. Replace the former `backend` string with this boolean; the old key is rejected. There is no warm-up: the first real numerical call imports Numba and loads or compiles its functions inside the measured solve. This setting affects DP only.

`energy_plan.preferred_reserve_kwh` is a positive, finite target of stored battery kWh measured from zero; it defaults to 4 kWh. Physical discharge cut-offs do not shift this scoring origin. `energy_plan.protection_preference` is X, any finite non-negative number, defaulting to 50 when omitted (also the shipped value). Values above 100 are supported. Replace the former `optimisation_weights` block with this scalar when updating separate YAML files; omitting the field now uses 50. The old battery share corresponds to X = 100 * maximise_battery / (maximise_profit + maximise_battery), but the revised objective changes its effect.

The maximum protection reward is `(X / 100) * reserve_kwh * reference_import_rate * projection_hours / 24`. Here `projection_hours` is the actual simulated duration. The reference is the time-weighted average import rate over the next 24 elapsed hours, clipped to zero if its mean is non-positive. Missing tariff coverage raises an error. The optimiser maximises reward minus net cost, using the existing diminishing-return curve and scenario probabilities normalised to sum to one. X = 0 is cost-only; X = 100 still considers cost. No reference solves or min/max score normalisation remain. Approximation error (0.05 protection points) is a fixed code constant. See [plan-model scoring](plan-model.md#3-scoring).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

`forecast.solar.arrays` has inline comments for each field and links to the [Forecast.Solar estimate API guidance](https://doc.forecast.solar/api:estimate). The official [API specification](https://api.forecast.solar/swagger.yaml) defines the request parameters: geographic coordinates, panel tilt, azimuth relative to south (east -90, south 0, west 90), and installed peak kWp (`peak_kw`). `panel_id` and `name` are local identifiers and are not API parameters.

## UI energy-plan overrides

The Plan settings YAML editor exposes the contents of energy_plan. It accepts a complete replacement, validates using the same normalisation and validation as file loading, and rejects unknown fields. Apply automatically reruns the plan using an optional per-browser EnergyPlanConfig without modifying global configuration. The schema-aware serializer follows the hand-written YAML layout: blank lines between sections and between the model/mix_integer_linear_prog/dynamic_programming entries within optimiser, inline scenario/refinement mappings, inline weekday/timestep lists and quoted HH:MM times. Consecutive identical timesteps collapse to minutes*count; separated runs stay separate and single steps remain numeric. Scheduled rules with identical start/end times, rate and optional name share one weekday list in Monday-to-Sunday order. Different names/rates/windows remain separate, including overnight windows; grouping changes representation rather than schedule behaviour. Deserialising the generated YAML restores equivalent settings, and serialising again produces the same text. Descriptive comment fields on scheduled rules are ignored by both file loading and editor parsing. Save To Disk atomically replaces only energy_plan in the startup file, preserving all surrounding text; comments within the section are replaced by the editor text. Saving does not reload the global config; future process starts read the saved settings.

Rename former energy_plan.optimiser_specific to energy_plan.optimiser; the old section name is rejected to prevent silently losing settings. optimiser.model selects mix_integer_linear_prog (default) or dynamic_programming. Generated YAML always includes this field with `# model - mix_integer_linear_prog, dynamic_programming` directly above it. The UI uses the override model; other callers use the global configured model. Save To Disk confirms before persisting, keeps the editor open and does not apply or run the draft.
