# Configuration

The [runtime implementation guide](../implementation/runtime.md) traces YAML normalisation, dataclass relationships, and which processes/components consume configuration.

Source: [config.yaml](../../config.yaml), [config.py](../../config.py). Configuration loads at process startup; a managed restart reloads it for both children.

## Sections

| Section | Purpose / shipped values |
| --- | --- |
| `timezone` | `Europe/Dublin`; local tariffs and date boundaries |
| `logging.level` | `INFO`; `DEBUG` adds read-query timing |
| `database.path` | `solar.db`, relative to process working directory |
| `actuals.modbus` | Host/port/timeout/default device ID; shipped port 502, default ID 247 |
| `actuals.data_retrival_schedule` | Live 1 second; full persisted/shared refresh 60 seconds |
| `forecast` | Endpoint, timeout, collection interval 3600 seconds, array definitions |
| `dashboard` | Host `0.0.0.0`, port 8090, PV-string-to-panel mapping |
| `tariffs.periods` | Effective-dated import/export schedules |
| `energy_plan` | 24-hour projection, 15-minute resolution, load-history settings, objective weights, scheduled battery limits |
| `additional_device_info` | Charge/discharge efficiencies, both shipped as 0.95 |

These are current file values, not universal defaults. Preserve exact configuration spellings, including `data_retrival_schedule` and `full_updated_interval_seconds`.

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

- [sigen_register_map.json](../../sigen_register_map.json) and [sigen_device_register_map.json](../../sigen_device_register_map.json) are source-relative fixed filenames.
- Explicit map device IDs override the default. Current maps distinguish plant 247 and inverter 1.
- Map `scale` is a **divisor** during decoding, separate from database fixed-point scaling.
