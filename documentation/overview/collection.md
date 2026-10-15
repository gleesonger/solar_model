# Data collection

For register definitions, wire decoding, tuple dictionaries, provider JSON examples, and the complete ingestion call chain, see [collection implementation](../implementation/collection.md).

Source: [main_data_collection.py](../../main_data_collection.py), [modbus_collector.py](../../modbus_collector.py), [forecast_collector.py](../../forecast_collector.py).

## Persisted scheduler

1. Load configuration/maps, open the database, and restore previous cumulative values.
2. Wait until the next wall-clock full interval.
3. Connect/reconnect Modbus and collect electrical values; then attempt device metadata.
4. If due, collect all forecast arrays and run operational adjustment.
5. Sleep until the next wall-clock boundary; repeat.

- Full collection is configurable, shipped at 60 seconds; it is not inherently fixed to one minute.
- Each cycle checks the persisted last forecast attempt against `forecast.interval_seconds`, using UTC elapsed time. Recent attempts skip HTTP requests and adjustment, including after restart; an INFO message gives the last attempt and next due time in the configured timezone.
- Existing installations without an attempt marker use the latest saved forecast collection. Due attempts are reserved before HTTP work; failed/partial attempts still consume the interval, preventing restart retry bursts. A database schedule-check failure skips HTTP and retries the check next cycle.
- Collectors expose `collect_once`; they do not run independent scheduling loops.
- No catch-up queue exists. Requests and correction run synchronously in the persisted collector.

## Modbus decoding

- Definitions include metric, raw address, kind, unit, data type, words, scale, cumulative flag, optional device ID/name.
- Definitions are grouped by device and register kind into spans of at most 120 registers. Spans may include unused gaps.
- `holding` uses holding-register reads; other kinds use input-register reads. Addresses are passed directly to pymodbus.
- Words decode in big-endian order; signed types use two's complement. Numeric result is divided by map `scale`; strings decode UTF-8 through the first NUL.
- The client tries the `device_id` argument, with a `slave` fallback for API compatibility.
- An error or short response rejects the electrical interval. The connection is closed and retried next cycle.
- Metadata failure retains an already saved electrical row. Empty/zero/non-finite metadata is discarded; valid changed values append history. Known inverter models add maximum PV input power from [datasheets.py](../../datasheets.py).

## Forecast collection

- Request path is endpoint/latitude/longitude/declination/azimuth/peak_kw.
- All arrays in a scan share one collection timestamp and wide forecast rows. Each array commits separately, so a failed request can leave a partial snapshot.
- HTTP status and provider message type are checked. A failed array logs an exception; remaining arrays still run.
- `refresh_adjusted_forecast` runs after all requests. Its failure preserves raw data and leaves adjusted values incomplete.
- See [forecasting.md](forecasting.md) for correction and maintenance; the old tree backfill is a different model.

## Live dashboard collection

- [dashboard/live.py](../../dashboard/live.py) uses its own client/thread and filtered register subset.
- It polls while a browser is connected, retains successful readings in memory, and retries on failure.
- It also reads daily energy/battery values and uses database baselines for grid/PV totals; it does not write samples.
- Shipped polling is 1 second plus request time. Failed reads retain the last value; [freshness warnings](dashboard.md) expose its age.
