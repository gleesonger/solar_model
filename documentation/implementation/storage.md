# Storage, derived samples, rollups, and retrieval

Source: [solar_model/storage/schema.py](../../solar_model/storage/schema.py). The [data-model overview](../overview/data-model.md) lists tables and invariants; this guide follows the values through actual persistence functions.

## 1. Database abstraction and decorators

```text
open_database(path)
  create_engine_for_database(path) -> cached SQLAlchemy Engine
  rename legacy forecast and battery columns
  create raw/device/metadata/solar-forecast tables if absent
  add missing columns; drop legacy JSON columns
  SolarDatabase(engine) -> engine + sessionmaker
  add missing columns if the hourly table exists
  initialize_hourly_rollup(): BEGIN IMMEDIATE
    create hourly table if absent
    check database_metadata completion marker
    rebuild uninitialized hourly history and commit marker atomically
  _migrate_house_load(): BEGIN IMMEDIATE
    check database_version; backfill house-load intervals and rebuild hourly history
    commit database_version = "0.2.0" with the derived data
  _migrate_house_load_forecast(): BEGIN IMMEDIATE
    create forecast_house_load; drop undeployed forecast_load
    commit database_version = "0.3.0" with the forecast-table replacement
```

`SolarDatabase.sessions()` creates a session; `sessions.begin()` owns a committing transaction. `close()` disposes the engine's pooled connections. Cached Engine identity is reused by path, but disposed engines can reconnect; a `SolarDatabase` is not a permanently open cursor.

| Representation | Measurement example | Timestamp example |
| --- | --- | --- |
| Decoder / ORM write | `plant_pv_power_kw = 2.5` | timezone-aware ISO string |
| Physical SQLite | integer `25000` | integer UTC epoch seconds |
| ORM read | float `2.5` | UTC ISO string |
| Raw SQL read | integer `25000` until explicitly divided | integer until parsed |

`ScaledInteger` binds `round(value * 10000)` and returns `value / 10000`; null stays null. The source alias `Float = ScaledInteger` means declarations that look like floats still store integers. `EpochSeconds` accepts integers directly or parses datetime/ISO values; naive values assume Europe/Dublin. It returns UTC ISO text when ORM-reading.

Electrical/forecast `collected_at_local` is a hybrid expression/property, not a stored column. Its local-time conversion uses the host timezone. Device history stores both timestamp strings directly and keeps `value` as text.

`ForecastHouseLoadSample` stores `target_day` (local ISO date), `forecast_time` (UTC epoch/ISO), `collected_at_utc` (generation epoch/ISO), and nullable `load_kwh` (scaled integer/physical float). The composite key is `(target_day, forecast_time)`. `has_house_load_forecast()` skips fitting an existing day; `save_daily_house_load_forecast()` serializes a second check and all hourly inserts with `BEGIN IMMEDIATE`. Concurrent or repeated writers cannot overwrite the first committed profile. Version 0.3.0 creates this table and drops the old undeployed `forecast_load` table in one transaction with the version update, without synthesizing historical house predictions.

## 2. Electrical sample construction

```text
save_modbus_sample(values, previous, collected, tariffs)
  normalise metric/unit to ORM column name
  calculate cumulative period deltas
  economic_period_values(..., collected local time)
  _add_pv_array_measurements(row, previous)
  transaction:
    add SigenStorModbusSample(**row)
    _add_hourly_sample(session, row)
  update previous only after commit
```

`column_name()` cleans names and appends a unit suffix if absent: `% -> percent`, `V -> volts`, `A -> amps`, and kW/kWh/Hz to lower-case suffixes. For example, decoded `plant_battery_soc` becomes `plant_battery_soc_percent`. Undefined electrical metrics are rejected against the declared model; register-map changes alone cannot extend storage.

Synthetic input state and resulting physical-unit row:

```python
# Abbreviated; previous is mixed-type despite its float annotation
previous = {
    "plant_pv_total_kwh": 100.0,
    "inverter_pv1_power_kw": 1.0,
    "_collected_at_utc": "2026-09-15T08:59:00+00:00",
}
# Current PV cumulative meter = 100.03; string V=400, A=5
row = {
    "collected_at_utc": "2026-09-15T09:00:00+00:00",
    "plant_pv_total_kwh": 100.03,
    "plant_pv_total_kwh_period": 0.03,
    "inverter_pv1_power_kw": 2.0,
    "inverter_pv1_energy_kwh_period": 0.025,  # (1+2)/2 × 1/60
    # tariff rates/band, costs, and other decoded metrics omitted
}
```

The cumulative delta measures counter movement; PV-string energy estimates a voltage/current-derived power integral. They need not match. Later UI allocation makes string proportions agree with the plant total.

### Electrical field families

| Family | Declared fields / meaning |
| --- | --- |
| Plant powers | `plant_grid_power_kw`, `plant_pv_power_kw`, `plant_battery_power_kw`, `plant_load_power_kw` |
| Battery state | `plant_battery_soc_percent`, `plant_battery_available_discharge_kwh`; readings, not energy flows |
| Plant cumulative meters | PV/load daily and lifetime; grid import/export lifetime; battery charge/discharge lifetime; each with a `_period` column |
| Inverter cumulative meters | Battery charge/discharge daily and associated `_period` columns |
| Inverter electrical readings | `inverter_power_kw`, average battery cell voltage, grid frequency, three-phase voltage/current |
| PV strings 1–4 | `inverter_pvN_voltage_volts`, `inverter_pvN_current_amps`, derived `inverter_pvN_power_kw`, derived `inverter_pvN_energy_kwh_period` |
| Economics | `import_tariff_band`, `import_rate`, `export_rate`, `grid_import_cost_period`, `grid_export_revenue_period`, `net_cost_period`, `no_solar_battery_import_cost_period` |

All measurement/economics fields are nullable. Only `collected_at_utc` identifies an electrical row; duplicate collection timestamps are not upserts.

### Derived-value rules

- House-load energy uses `house_load_energy(previous_kw, current_kw, elapsed_seconds)` in both collection and migration. Consecutive finite load powers with `0 < elapsed_seconds <= 120` produce trapezoidal kWh; other intervals are null. Previous load power is restored on restart and explicitly cleared when the current reading is missing. Like other stored period fields, energy is attributed to the ending sample, including midnight/hour boundaries; it is not split across buckets. The comparison research script instead splits at local midnight and does not round each interval, so small differences in daily/monthly totals are expected.
- Meter delta is current minus previous when both are available and current ≥ previous; otherwise null. A decrease is a reset/unknown interval, not zero.
- Each of four PV powers is `max(V*A, 0)/1000`, or null if either reading is absent.
- Each PV energy is trapezoidal power × elapsed UTC hours when prior/current powers and a positive elapsed time exist; otherwise null. No maximum-gap cutoff is applied.
- Economics receives grid import/export/load deltas, picks the tariff for the sample's local end time, and writes physical money/rate values. Net cost is null unless both import cost and export revenue are known.
- `previous` reloads the newest row's cumulative fields, timestamp, and PV powers after restart. During operation it is expanded from decoded values after each successful transaction.

## 3. Hourly rollup structure

`SigenStorHourlySample.__table__` dynamically mirrors electrical numeric columns, replacing its timestamp with `hour_start_utc`, adding `sample_count`, sign-separated grid-power sums and their valid observation count, and making tariff band part of the key.

```python
# Physical-unit ORM representation, synthetic three samples
{
    "hour_start_utc": "2026-09-15T09:00:00+00:00",
    "import_tariff_band": "day",
    "sample_count": 3,
    "plant_pv_power_kw": 6.0,             # sum of powers, not mean
    "plant_pv_total_kwh_period": 0.10,   # sum of measured energy
    "plant_pv_total_kwh": 100.10,        # maximum cumulative meter
    "import_rate": 1.2,                 # sum of three rate readings
    ...,
}
```

`_add_hourly_sample()` floors UTC epoch seconds to an hour, replaces a missing band with `Unknown`, and uses SQLite conflict-update on `(hour_start_utc, import_tariff_band)`.

- `sample_count` increments for every source row.
- Each nullable non-counter/non-period measurement has an integer `<field>_sample_count` from `HOURLY_VALID_COUNT_COLUMNS`; only non-null values increment it. Grid uses its separate `grid_power_sample_count`. Zero counts as observed; missing does not.
- Fields in `CUMULATIVE_COLUMNS` merge by maximum.
- All other numeric fields merge by addition.
- A null incoming/current side preserves the non-null side; both missing remain null.
- Grid import/export sums add `max(grid_kw, 0)` / `max(-grid_kw, 0)` per raw sample, before opposite signs can cancel. `grid_power_sample_count` counts non-null grid observations, including zero; absent grid observations leave both sums null and count zero.
- This upsert shares the raw-sample transaction.

Grid import/export power means use `plant_grid_import_power_kw` / `plant_grid_export_power_kw` divided by `grid_power_sample_count`, combined across tariff bands. The signed `plant_grid_power_kw` sum is retained separately and must not be split after summation. These fields are power statistics, not imported/exported meter energy.

Hourly **energy/cost** is already an aggregate. Hourly **power/rate/SOC/available energy** divides each sum by that field's valid count. Thus 4/NULL averages to 4, 4/0 to 2, and all-null remains missing. Combine sums and counts across tariff bands before dividing.

`_insert_hourly_rollup()` reproduces sums/maxima and per-field valid counts using SQL grouped by hour and band. Startup `initialize_hourly_rollup()` uses explicit SQLite `BEGIN IMMEDIATE`: table creation (if needed), full replacement and metadata key `hourly_rollup_initialized_v3 = "1"` commit together. Interrupted work rolls back and retries. V3 requires valid counts as well as v2's split grid statistics; earlier markers do not bypass the upgrade. Databases without v3 receive one full rebuild even if hourly rows exist. A present marked table skips reaggregation; a missing table is rebuilt even if a marker survives. The lock serializes initialization with collector writes. This is a readiness marker, not a global schema version.

Manual `backfill_hourly_rollup()` still inserts missing groups with `OR IGNORE`; it does not repair stale existing groups. `rebuild_hourly_rollup(start,end,zone,connection=None)` replaces whole UTC hours overlapping the inclusive local-date range, including neighboring-date samples in boundary hours. It owns a transaction unless a caller supplies one; tariff recalculation supplies its raw-update connection. Adding unrelated columns alone does not backfill derived historical data.

The separate `database_version` metadata key controls the house-load migration: `0.1.0` or absent upgrades to `0.2.0`; startup then replaces the undeployed forecast table and advances to `0.3.0`. Unsupported values are rejected before schema changes. After raw/hourly missing-column additions and hourly readiness initialization, `_migrate_house_load()` locks writers, reads ordered raw power samples, computes and rounds interval energy using the same helper as collection, writes only the new raw energy field, replaces hourly aggregates, and updates the version in one transaction. Failure rolls back data/version; already-added nullable columns can remain and are safe on retry. Versions `0.2.0` and `0.3.0` skip this backfill. This version is independent of the existing v3 hourly readiness marker.

## 4. Forecast persistence and adjusted writers

`claim_forecast_collection(now, interval_seconds)` returns `(due, previous_attempt_or_collection)` using aware UTC datetimes. It checks `database_metadata.forecast_last_attempt_utc` and the latest saved collection, reserves a due attempt in one short `BEGIN IMMEDIATE` transaction, and releases the lock before HTTP. The ISO-text attempt marker persists even if requests fail or the process stops. It is separate from forecast rows and hourly readiness; no schema change is required.

Raw writes merge a panel's three fields into existing wide rows. Adjusted writes accept dictionaries of **physical values**, never pre-scaled integers:

```python
database.save_adjusted_forecasts(
    collection_iso,
    {
        target_iso: {
            "panel_1_adj_watts": 2200.0,
            "panel_1_adj_watt_hours": 4400.0,
            "panel_1_adj_watt_hours_day": 18000.0,
            # other panels as required
        }
    },
)
```

The writer checks that keys belong to `_adj_` model columns and that every target already has a raw row. One snapshot update commits atomically; the bulk variant uses `collection -> target -> fields` for multiple snapshots in one transaction. Power and later energy passes are separate calls/transactions in the live model.

`pending_adjusted_forecasts()` selects a row if at least one panel has raw watts but null adjusted watts; it returns dictionaries of **ORM-decoded fields**, including ISO timestamp keys and physical values. Pending snapshot lookup is distinct/oldest-first. These APIs support the older tree backfill; production correction loads frames directly rather than polling pending rows.

## 5. Device-value change history

```text
save_device_info(readings, collected, variable_names)
  optional inverter model lookup -> derived Maximum PV input power
  device_value_text() -> valid text values
  read previous device rows, newest id first
  choose latest per human-readable variable
  append only changed value/unit pairs
```

Typical ORM row: `{id, collected_at_utc, collected_at_local, variable_name, variable, value, unit}`. `variable_name` is a stable identifier used by planning; `variable` is a human label. Empty strings, text `"0"`, numeric zero, and non-finite numeric values are not saved. This history is not a complete metadata snapshot at every electrical timestamp.

## 6. Retrieval contracts and consumers

| Reader | Source/selection | Returned representation |
| --- | --- | --- |
| `load_previous_cumulatives` | Latest `sigenstor` by collection time | Mixed-value previous-state dict |
| Recent actual loaders | Local-day `sigenstor_hourly` rows | ORM physical values → 24-hour frames |
| Analysis SQL | Selected hourly/minute/forecast fields in bounded CTEs | Explicitly normalised SQL rows → chart dictionaries |
| Operational model | All forecast vintages projected to collection/target plus four raw-watt fields; non-null PV readings averaged by UTC hour in SQL | Scaled forecast fields plus physical hourly actuals → model helper columns |
| Device UI | Device history descending ID | Newest-per-variable list of label/value/unit dicts |
| As-of planner | Cutoff-selected sample/vintage/device history | ORM physical values → typed device/interval objects |
| Data export | SQL-aggregated bounded electrical samples and selected first-per-day forecast vintages | Explicit fixed-point normalisation → readable-column DataFrame |

No foreign keys link these tables. Their relation is temporal and metric-based. Model opening/migration and reader creation are different operations: many dashboard reads use `create_engine_for_database()` directly and do not trigger schema migration.

Next: [forecast correction](forecasting.md), [dashboard queries](dashboard-data.md), or [planning](planning.md).

## Package ownership

`schema.py` declares the ORM tables and scaling types. `connection.py` owns cached engines, SQL timing and `SolarDatabase`; its measurement and forecast methods come from `measurements.py` and `forecasts.py`. `migrations.py` owns `open_database` and legacy upgrades. This split preserves the same transactions and migration/backfill order.

`queries/forecasting.py` loads calibration frames; `queries/planning.py` selects as-of planning inputs; `queries/recent.py`, `analysis.py` and `exports.py` own native dashboard aggregation. `queries/dashboard.py` retains dataframe adapters and reference calculations. `queries/types.py` defines data records without importing NiceGUI. Presentation helpers live in `dashboard/formatting.py`.
