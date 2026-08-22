# Solar model collectors

The single-threaded scheduler writes both collectors to `solar.db`:

```powershell
python main.py
```

`main.py` synchronizes to the next wall-clock minute, runs Modbus first every minute, and runs Forecast.Solar every 15 minutes when due. A failed interval is logged and skipped. The collector modules expose one-pass functions and are not standalone schedulers.

Set `scheduler.interval_seconds` in `config.yaml` to change the scheduler interval; it defaults to `60` seconds.
Set `forecast.interval_seconds` in `config.yaml` to change the minimum elapsed time between forecast scans; it defaults to `900` seconds.

SQLite schema creation and all database writes are handled by `database.py`. The collectors only fetch, decode, and pass records to that module.

## Forecast

The arrays are parameterized in `config.yaml`:

| Name | Latitude | Longitude | Declination | Azimuth | Peak |
| --- | ---: | ---: | ---: | ---: | ---: |
| West | 53.4508401 | -6.15367 | 35 | 112.5 | 5.225 kWp |
| East | 53.4508401 | -6.15367 | 35 | -67.5 | 6.175 kWp |

The API returns cumulative `watt_hours` values for forecast timestamps and daily totals. Forecast.Solar public access may return hourly data; 15-minute data depends on the account plan.

## Modbus register map

Sigenergy register addresses and scaling are firmware/device dependent. The collector reads contiguous groups. The supplied `sigen_register_map.json` contains the documented plant and inverter metrics used by this project.

Each entry has this shape:

```json
{
  "metric": "battery_power_w",
  "address": 1234,
  "kind": "holding",
  "unit": "W",
  "scale": 0.1,
  "cumulative": false
}
```

Use `input` or `holding` for `kind`. Addresses are passed to pymodbus as zero-based addresses. For each metric marked `cumulative`, `period_kwh` is calculated from the previous successful sample, including the latest persisted value after a restart; counter resets and decreases produce `NULL`.

The requested device defaults are Modbus TCP host `192.168.1.130` and port `502`. The supplied map uses the Sigenergy plant unit ID `247` and inverter unit ID `1`; set explicit `device_id` values in `sigen_register_map.json` when your topology differs. Set `database.path` in `config.yaml` to change the SQLite path.

The database uses explicit SQLAlchemy models and wide tables: `modbus_samples_wide` has one row per Modbus sample with one column per metric, and `forecast_samples_wide` has one row per forecast timestamp with columns for each solar array. Each forecast scan creates a collection GUID; every array in that scan uses the same GUID, and the GUID plus forecast timestamp identifies the shared wide row. Modbus column names include their units, for example `plant_pv_power_kw` and `inverter_battery_soc_percent`; cumulative metrics also have a `_period` column. Both tables store UTC/local timestamps.
