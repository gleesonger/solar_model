# Data model and invariants

For sample derivation, transaction/rollup behavior, and reader contracts, see [storage implementation](../implementation/storage.md). The [schema reference](../reference/schema.md) lists every declared field.

Source: [database.py](../../database.py), especially `ScaledInteger`, `EpochSeconds`, and `SolarDatabase`.

For a consolidated comparison of units, signs, missing values, time attribution and chart/download aggregation, see [metric semantics](../reference/metric-semantics.md).

## Tables

| Table | Key | Meaning |
| --- | --- | --- |
| `sigenstor` | `collected_at_utc` | One successful electrical sample; telemetry, cumulative meters, interval energy/costs, derived PV-string values |
| `sigenstor_hourly` | `hour_start_utc`, `import_tariff_band` | UTC-hour aggregates, split by import tariff band; missing band becomes `Unknown` |
| `forecast_solar` | `collected_at_utc`, `forecast_time` | One collection vintage and target time; up to four panels' raw/adjusted W and Wh fields |
| `forecast_load` | `target_day`, `forecast_time` | One fixed hourly load profile per configured-local date; generation timestamp and nullable kWh |
| `sigenstor_devices` | integer `id` | Append-only valid device-value changes, with variable/name/value/unit and ISO UTC/local timestamps |
| `database_metadata` | `key` | Key/value storage including hourly-initialization completion; no global database version |

## Units and time

- Measurement/rate/cost columns are **stored as physical value × 10,000, rounded to integer**. ORM reads divide automatically; raw SQLite reads do not.
- Example: stored `plant_pv_power_kw = 25000` means **2.5 kW**. Stored forecast watts need both `/10000` and `/1000` to become kW.
- `sigenstor`/`forecast_solar`/hourly timestamps are integer UTC epoch seconds. Their ORM API exposes ISO strings.
- Device history uses ISO text timestamps and text values, not the measurement decorators.
- Local dates should use `config.timezone`. Model local-time hybrids instead use the host timezone; naive `EpochSeconds` inputs assume Europe/Dublin. See findings.
- kW is instantaneous power; kWh is energy. Forecast W/Wh fields retain provider units.
- Stored grid power is positive for import and negative for export; dashboard split import/export series are both non-negative magnitudes. Summary battery energy is charge minus discharge; signed instantaneous battery power is passed through from the device.

## Derived values

- Cumulative `_period` values are differences from the previous successful meter value. First readings and decreases/resets produce `NULL`, not negative energy.
- Restart loads previous cumulatives and PV powers from the latest persisted sample. Gaps therefore span the time since that sample.
- PV-string power is `max(voltage × current, 0) / 1000`; interval energy uses the trapezoidal average of previous/current power and elapsed UTC hours.
- Costs use the tariff at the collection timestamp for the whole measured interval. Gaps and tariff boundaries are not split into smaller billing intervals.
- Electrical rows and hourly updates commit together. Previous in-memory readings advance only after success. Device history is saved separately.
- Hourly grid import/export power stores separate sign magnitudes and a valid grid observation count, preserving activity even when net power is zero. These are distinct from cumulative meter energy.
- Hourly cumulative meters use maxima; other numeric fields use sums. Means divide by each field's non-null count (`<field>_sample_count`; grid uses `grid_power_sample_count`). Zeros count as observations; summed power is not an hourly power reading.
- Missing measurements remain nullable. Some display paths substitute zeros; this must not be mistaken for observed zero generation.
- Daily load forecasts store one atomically inserted profile per local date, with UTC hourly targets and a separate generation timestamp. They remain fixed across collection runs and restarts; missing history stays null and no historical profiles are backfilled.
- Raw and adjusted forecast fields stay separate. Adjusted writers accept only `_adj_` fields; raw collection can update its own panel fields at the same composite key.

## Migration behavior

- `open_database()` renames legacy forecast fields to `_raw_`, renames battery availability to the plant-level name, creates tables, adds missing columns, and drops legacy JSON columns.
- Conflicting duplicate raw/legacy forecast values cause migration refusal rather than silent data loss.
- Initial hourly creation/rebuild and `hourly_rollup_initialized_v3` commit together. Older v1/v2 or unmarked history rebuilds once to populate valid counts; interrupted initialization retries. Later marked opens skip the scan. Adding unrelated columns alone does not trigger another rebuild.
- Schema opening can write substantial data. Back up before deployment; additive schema changes do not automatically derive historical values for new metrics.
