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

These are current file values, not universal defaults. Preserve exact configuration spellings, including `data_retrival_schedule` and `full_updated_interval_seconds`.

## Forecast model calibration parameters

`forecast.solar` groups provider `endpoint`, `timeout_seconds`, collection `interval_seconds`, `arrays`, and `model_calibration_parameters` containing `day_ahead` and `intraday`. `forecast.load.model_calibration_parameters` holds house-load tuning. Each uses `lookback`, `halflife`, and `prior_effective_observations`; see the [forecasting defaults table](forecasting.md). `lookback`/`halflife` use days for day-ahead/load and hours for intraday. Intraday `lookback: null` preserves all earlier completed hours today. Day-ahead `prior_effective_observations: null` reflects that model's absence of prior shrinkage; a numeric value is rejected rather than ignored.

`prior_effective_observations` is a decimal fraction, not a count: load defaults to `0.2` (20%) and intraday to one-third. The shipped YAML uses `0.3333` for intraday. The derived strength is `1 / prior_effective_observations - 1` (4 for load and approximately 2 for intraday). Values must be finite and in `(0, 1]`; old percentage values such as `20` are rejected. Lookback/halflife values must be finite and positive, except the permitted intraday null lookback. Load lookback and both load/intraday fractions are required. Within the new solar/load sections, omitted calibration blocks inherit defaults; a supplied model block must provide its three fields.

Deployed YAML files must move the former top-level forecast provider/array settings into `forecast.solar`, move `solar_day_ahead`/`solar_intraday` calibration blocks to `forecast.solar.model_calibration_parameters.day_ahead`/`intraday`, and move load calibration to `forecast.load.model_calibration_parameters`. The old flat forecast layout is no longer accepted.

The live collector passes these settings to solar correction and the fixed daily load forecast. `python -m solar_model.tools.evaluate_load --config config.yaml ...` and `python -m solar_model.tools.rebuild_forecasts --config config.yaml ...` load the same settings. Both CLIs use the configured database/timezone unless overridden by `--database`/`--timezone`. The `forecast.load.model_calibration_parameters` tuning block now controls house-load forecasting in both collection and planning. The unused `energy_plan.load_forecast_historical_lookback_window_days` and `load_forecast_historical_lookback_halflife_days` fields have been removed; use this shared forecast tuning for planning too.

## Validation and parsing

- YAML is deserialised through dacite into frozen dataclasses; lists become tuples. Required dataclass fields must be present.
- Unknown keys are currently ignored: `dacite.Config` does **not** enable strict mode. This permits descriptive `comment`/period `name` keys but can hide misspellings.
- Optional planning-rule tuples default empty; minimum battery also accepts `{comment: ..., rules: [...]}`. The optimiser currently still requires usable minimum/maximum rules.
- Intervals/timeouts/history lengths must be positive. `energy_plan.projection_resolution_mins` is a YAML list of positive integer minutes, for example `[15, 15, 15, 15, 60, 60]`, covering at least `num_projection_hours`. Replace the former scalar value in deployed YAML files. The shipped vector is 24 quarter-hour entries followed by 18 hourly entries. Its first entry is the intact next action; subsequent cumulative boundaries align from the preceding full UTC hour, dropping boundaries overtaken by the first action. A final partial interval preserves the full elapsed horizon. At 10:05 the shipped vector produces 43 periods. Tariff, battery-limit and midnight boundaries do not add splits.
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

## Planning score

`energy_plan.preferred_reserve_kwh` is a positive, finite target of stored battery kWh measured from zero; it defaults to 4 kWh. Physical discharge cut-offs do not shift this scoring origin. `energy_plan.protection_preference` is X, any finite non-negative number, defaulting to 50 when omitted (also the shipped value). Values above 100 are supported. Replace the former `optimisation_weights` block with this scalar when updating separate YAML files; omitting the field now uses 50. The old battery share corresponds to X = 100 * maximise_battery / (maximise_profit + maximise_battery), but the revised objective changes its effect.

The maximum protection reward is `(X / 100) * reserve_kwh * reference_import_rate * projection_hours / 24`. Here `projection_hours` is the actual simulated duration. The reference is the time-weighted average import rate over the next 24 elapsed hours, clipped to zero if its mean is non-positive. Missing tariff coverage raises an error. The optimiser maximises reward minus net cost, using the existing diminishing-return curve and scenario probabilities normalised to sum to one. X = 0 is cost-only; X = 100 still considers cost. No reference solves or min/max score normalisation remain. Approximation error (0.05 protection points) is a fixed code constant. See [plan-model scoring](plan-model.md#3-scoring).

`projection_resolution_mins` also supports repetition entries: `[15*24, 60*18]` expands to 24 quarter-hour steps followed by 18 hourly steps. Plain integer entries can be mixed with repetitions. Entries accept `mins*count` or just `mins` (for example `15*24`, `15` or `"15"`). A plain duration means one step. Durations and explicit counts must be positive integers; a trailing `*` without a count is rejected. Expansion happens once during YAML loading; the planner still receives the same tuple of minutes and applies its usual alignment.

`forecast.solar.arrays` has inline comments for each field and links to the [Forecast.Solar estimate API guidance](https://doc.forecast.solar/api:estimate). The official [API specification](https://api.forecast.solar/swagger.yaml) defines the request parameters: geographic coordinates, panel tilt, azimuth relative to south (east -90, south 0, west 90), and installed peak kWp (`peak_kw`). `panel_id` and `name` are local identifiers and are not API parameters.
