# Collection and external data ingestion

Sources: [solar_model/integrations/sigenstor/registers.py](../../solar_model/integrations/sigenstor/registers.py), [solar_model/services/forecasting.py](../../solar_model/services/forecasting.py), [solar_model/runtime/collector.py](../../solar_model/runtime/collector.py), both [electrical](../../solar_model/integrations/sigenstor/register_maps/sigen_register_map.json) and [device](../../solar_model/integrations/sigenstor/register_maps/sigen_device_register_map.json) register maps.

## 1. Full-interval call hierarchy

```text
solar_model.runtime.collector.main()
  load_registers(electrical_map, default_device_id)
  load_registers(device_map, default_device_id)
  database.load_previous_cumulatives()
  wait for next full interval
  each cycle:
    connect if client is None
    solar_model.services.collection.collect_once(...)
      read_registers(electrical blocks)
        read_block() -> pymodbus result
        decode_registers() / decode_string()
      timestamps()
      database.save_modbus_sample(...)
      read_registers(device blocks)
      database.save_device_info(...)
    solar_model.services.forecasting.collect_once(..., interval_seconds)
      database.claim_forecast_collection(now, interval_seconds)
      if recent: log last attempt/next due; return without HTTP or adjustment
      if due:
        timestamps() once for all arrays
        each array: fetch_array() -> database.save_forecast()
        refresh_adjusted_forecast()
    sleep_until_next_interval()
```

Collectors receive the database abstraction; they do not define table layouts. Electrical persistence and derived interval calculations are explained in [storage.md](storage.md), correction in [forecasting.md](forecasting.md).

The due check atomically reserves `database_metadata.forecast_last_attempt_utc` before network work, using UTC elapsed seconds. It also considers the latest saved forecast collection for compatibility with installations lacking that key. The most recent time wins. Reservation commits before HTTP, so failures, partial scans and process restarts cannot trigger another attempt within the configured interval. Concurrent claimants serialize under `BEGIN IMMEDIATE`; database-check failure logs and skips the request until another cycle. Skip logs include last attempt and next due in the configured timezone. There is no catch-up replay or immediate retry for failed arrays.

## 2. Register definitions become read plans

Example of an actual map definition's shape:

```json
{
  "metric": "plant_grid_power_kw",
  "address": 30005,
  "kind": "input",
  "unit": "kW",
  "data_type": "s32",
  "scale": 1000,
  "cumulative": false,
  "words": 2,
  "device_id": 247
}
```

`Register` is a frozen dataclass. Required fields are metric/address/kind/unit; optional defaults are `data_type='u16'`, `scale=1.0`, `cumulative=False`, `words=1`, and nullable device/name fields. `load_registers()` fills absent device IDs from configuration and optionally filters by a requested set of metric names. A missing requested metric or empty resulting map raises an error.

`group_registers()` groups by `(device_id, kind)`, sorts by address, and builds spans no longer than 120 words. It may read unconfigured addresses between definitions. The output contract is:

```python
list[RegisterBlock]
# Each block:
RegisterBlock(
    device_id=247, kind="input", address=30005, count=2,
    registers=(Register(...),),
)
```

The block's `registers` contains only definitions to decode, not every address in its span. Decoder offset is `definition.address - block.address`; `words` selects a slice from the response.

## 3. Wire data to decoded readings

`read_block()` selects holding-register reads for `kind == 'holding'`, otherwise input-register reads, and passes addresses unchanged. It first calls with `device_id`, then retries with `slave` if the library rejects that keyword.

The relevant pymodbus result interface is `isError()` and `registers: list[int]`. Individual words are unsigned 16-bit integers; definitions determine whether the assembled value is signed. An error or short slice fails the entire `read_registers()` call.

```python
# Synthetic s32 wire example, two words, scale divisor 1000
raw_words = [0, 2500]
decode_registers(raw_words, "s32") == 2500
physical_value = 2500 / 1000  # 2.5 kW

# read_registers() output, abbreviated
{
    "plant_grid_power_kw": (2.5, "kW", False),
    "plant_pv_total_kwh": (1234.56, "kWh", True),
    "plant_battery_soc": (62.0, "%", False),
    "inverter_pv1_voltage": (400.0, "V", False),
    "inverter_pv1_current": (5.0, "A", False),
}
```

The dictionary contract is `metric -> (physical value, unit, cumulative flag)`. Map `scale` is a divisor, **not** SQLite's later ×10,000 storage factor. Numeric words assemble big-endian; types beginning with `s` use two's complement. Strings concatenate big-endian bytes, terminate at NUL, decode UTF-8, and strip whitespace.

`collect_once()` rejects text in electrical readings and narrows to `dict[str, tuple[float, str, bool]]`. Device readings may include text. Timestamping occurs after electrical registers are read, so readings in a large block set are not an atomic hardware snapshot.

## 4. Persistence handoff and failures

The handoff to `save_modbus_sample()` contains decoded tuples, the mutable `previous` state, timestamp pair, and typed tariffs. The collector does not compute deltas or pre-scale stored integers.

After that write succeeds, device collection reads another block set. A `variable_names` dictionary maps human-readable device metrics to stable machine names; maximum PV input power receives a derived machine name. Device-history persistence is separate from the electrical transaction.

| Failure boundary | Result |
| --- | --- |
| Connect/read/decode/electrical write | `collect_once` returns `False`; scheduler closes client and retries next cycle |
| Device metadata read/write | Electrical row is retained; metadata failure is logged |
| One provider array | That array is skipped; remaining arrays still run |
| Operational correction | Already committed raw forecast remains; adjusted fields may be incomplete |

Previous electrical values advance after database commit. There is no fabricated sample for a missed interval. A later meter delta spans the gap; power-derived energy uses elapsed time from the previous sample.

## 5. Provider JSON to a forecast vintage

`fetch_array()` constructs:

```text
{endpoint}/{latitude}/{longitude}/{declination}/{azimuth}/{peak_kw}
```

It checks HTTP status, parses JSON, and rejects a provider `message.type` other than absent/`success`. The persistence layer reads only the following maps; additional response metadata is not stored as a raw JSON blob:

```json
{
  "result": {
    "watts": {
      "2026-09-15T09:00:00+01:00": 2000,
      "2026-09-15T10:00:00+01:00": 3000
    },
    "watt_hours": {
      "2026-09-15T09:00:00+01:00": 2500,
      "2026-09-15T10:00:00+01:00": 5500
    },
    "watt_hours_day": {"2026-09-15": 18000}
  },
  "message": {"type": "success"}
}
```

This is an illustrative shape, not a captured provider payload. W is predicted power, Wh is provider cumulative energy, and daily Wh is a date-keyed total.

`save_forecast()` walks the sorted union of watts/Wh timestamp keys. It sets daily Wh from the target text's first ten characters. A daily-only key with no timestamp in watts/Wh creates no row. Missing map entries become `None`.

For panel 1 the physical-unit values handed to the ORM are:

```python
{
    "collected_at_utc": collection_utc_iso,
    "forecast_time": target_iso,
    "panel_1_raw_watts": 2000,
    "panel_1_raw_watt_hours": 2500,
    "panel_1_raw_watt_hours_day": 18000,
}
```

Other panels populate their columns in the **same** `(collection, target)` row. Each array transaction commits separately. If arrays return different timestamp sets or a request fails, some panel columns remain null; a collection timestamp does not guarantee completeness.

The common scan timestamp identifies the vintage. It is different from forecast target time and persists through raw collection, adjusted writes, evaluation, and as-of planning selection.

## 6. Parallel live input path

The dashboard's live collector reuses `load_registers(metrics=...)` and `read_registers()`, but uses its own Modbus connection. It requests plant powers, battery availability/SOC, selected daily/lifetime meters, and voltage/current for mapped strings. It transforms tuples into `LivePowerData` and retains it in memory rather than calling `save_modbus_sample()`.

See [dashboard-ui.md](dashboard-ui.md) for its values dictionary, baselines, thread state, daily resets, and presentation handoff.
