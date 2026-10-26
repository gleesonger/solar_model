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
| `energy_plan` | 24-hour projection, 15-minute resolution, load-history settings, objective weights, scheduled battery limits |
| `additional_device_info` | Charge/discharge efficiencies, both shipped as 0.95 |

These are current file values, not universal defaults. Preserve exact configuration spellings, including `data_retrival_schedule` and `full_updated_interval_seconds`.

## Forecast model calibration parameters

`forecast.model_calibration_parameters` contains `solar_day_ahead`, `solar_intraday`, and `load`. Each uses `lookback`, `halflife`, and `prior_effective_observations`; see the [forecasting defaults table](forecasting.md). `lookback`/`halflife` use days for day-ahead/load and hours for intraday. Intraday `lookback: null` preserves all earlier completed hours today. Day-ahead `prior_effective_observations: null` reflects that model's absence of prior shrinkage; a numeric value is rejected rather than ignored.

`prior_effective_observations` is a decimal fraction, not a count: load defaults to `0.2` (20%) and intraday to one-third. The shipped YAML uses `0.3333` for intraday. The derived strength is `1 / prior_effective_observations - 1` (4 for load and approximately 2 for intraday). Values must be finite and in `(0, 1]`; old percentage values such as `20` are rejected. Lookback/halflife values must be finite and positive, except the permitted intraday null lookback. Load lookback and both load/intraday fractions are required. Older YAML files without `model_calibration_parameters`, or with omitted model blocks, inherit defaults; a supplied model block must provide its three fields.

Rename the former `forecast.models` key to `forecast.model_calibration_parameters` in separately deployed configuration files.

The live collector passes these settings to solar correction and the fixed daily load forecast. `python -m solar_model.tools.evaluate_load --config config.yaml ...` and `python -m solar_model.tools.rebuild_forecasts --config config.yaml ...` load the same settings. Both CLIs use the configured database/timezone unless overridden by `--database`/`--timezone`. The retained `load` tuning block now controls house-load forecasting in both collection and planning. Legacy `energy_plan` load-history fields remain accepted but no longer control the planning profile.

## Validation and parsing

- YAML is deserialised through dacite into frozen dataclasses; lists become tuples. Required dataclass fields must be present.
- Unknown keys are currently ignored: `dacite.Config` does **not** enable strict mode. This permits descriptive `comment`/period `name` keys but can hide misspellings.
- Optional planning-rule tuples default empty; minimum battery also accepts `{comment: ..., rules: [...]}`. The optimiser currently still requires usable minimum/maximum rules.
- Intervals/timeouts/history lengths must be positive. Projection duration must divide exactly by its resolution.
- Weights must be finite/non-negative and not both zero; efficiencies must be in `(0, 1]`.
- Validation checks timezone/log level, URL shape, ports/device ID, coordinates/orientation, supported PV strings, and mapped panel IDs.
- Array IDs are unique integers 1–4; names are labels. Empty arrays/mapping and non-positive `peak_kw` are not explicitly rejected.

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

`energy_plan.preferred_reserve_kwh` is a positive, finite soft reserve of usable energy above the physical discharge cut-off; it defaults to 4 kWh. It affects the diminishing-return protection curve, not feasibility. `optimisation_weights.maximise_profit` and `maximise_battery` now mix reference-normalised financial/protection scores; their normalised battery share is the protection preference (shipped 0.05). They no longer mix currency with tariff-valued stored energy. Approximation error (0.05 score points) and reference tolerance (`1e-6`) are fixed code constants.
