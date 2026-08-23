# Solar model collectors

The minute-resolution electrical and forecast collector writes to `solar.db`:

```powershell
python main_data_collection.py
```

`main_data_collection.py` synchronizes to the next wall-clock minute, runs Modbus first every minute, and runs Forecast.Solar at its configured interval when due. A failed interval is logged and skipped. The collector modules expose one-pass functions and are not standalone schedulers.

The lightweight live-power collector reads only the five Modbus power values used by the dashboard's `Latest (kW)` column:

```powershell
python main_live_collection.py
```

It defaults to a five-second interval and atomically replaces the single row in `sigenstor_live` after each successful read.

Set `actuals.scheduler.interval_seconds` in `config.yaml` to change the scheduler interval; it defaults to `60` seconds.
Set `live.scheduler.interval_seconds` to change the live-power interval; it defaults to `5` seconds.
Set `forecast.interval_seconds` in `config.yaml` to change the minimum elapsed time between forecast scans; it is configured for `3600` seconds.

Configuration loading is strict: every documented field must be present, unknown fields and incorrect types are rejected, and invalid values cause the process to exit during startup.

SQLite schema creation and all database writes are handled by `database.py`. The collectors only fetch, decode, and pass records to that module.

## Forecast

The arrays are parameterized in `config.yaml`:

| Panel ID | Name | Latitude | Longitude | Declination | Azimuth | Peak |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | East | 53.4508401 | -6.15367 | 35 | -67.5 | 6.175 kWp |
| 2 | West | 53.4508401 | -6.15367 | 35 | 112.5 | 5.225 kWp |

The API returns cumulative `watt_hours` values for forecast timestamps and daily totals. Forecast.Solar public access may return hourly data; 15-minute data depends on the account plan.

Each forecast array requires a unique integer `panel_id` from `1` through `4`; its `name` is only used as a display label. `dashboard.actuals_to_forecast` associates inverter PV inputs with those panel IDs. For example, `pv1: 1` compares inverter string PV1 with the forecast configured with `panel_id: 1`.

## Modbus register map

Sigenergy register addresses and scaling are firmware/device dependent. The collector reads contiguous groups. The supplied `sigen_register_map.json` contains electrical metrics, while `sigen_device_register_map.json` contains the device and system metadata recorded when values change. Relative register-map paths are resolved from the application source directory; the maps are not copied into the runtime configuration directory.

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

The requested device defaults are Modbus TCP host `192.168.1.130` and port `502`. The supplied maps use the Sigenergy plant unit ID `247` and inverter unit ID `1`; set explicit `device_id` values in the register maps when your topology differs. Set `database.path` in `config.yaml` to change the SQLite path.

The database uses explicit SQLAlchemy models. `sigenstor_samples` has one row per Modbus electrical sample with one column per metric, `sigenstor_live` contains only the latest live power sample, and `sigenstor_devices` records valid device-information values only when a variable changes. The inverter model is matched against the official SigenStor model catalogue to add its nameplate maximum PV input power. `forecast_solar_samples` has one row per forecast timestamp with columns for `panel_1` through `panel_4`. Each forecast scan creates a collection GUID; every panel in that scan uses the same GUID, and the GUID plus forecast timestamp identifies the shared wide row. Modbus column names include their units, for example `plant_pv_power_kw` and `inverter_battery_soc_percent`; cumulative metrics also have a `_period` column. All tables store UTC/local timestamps.

## Dashboard

Install the requirements and start the NiceGUI dashboard with:

```powershell
python main_dashboard.py
```

It listens on `dashboard.port` from `config.yaml`; `DASHBOARD_PORT` can override the configured value. The dashboard updates the `Latest (kW)` cells in place from `sigenstor_live` every five seconds. The remaining current-day, forecast, chart, and device data stays on the minute refresh; table rows and chart options are updated in place without recreating the tabs or their contents. Mapped PV-string voltage and current readings provide the per-panel actual energy and power values. The battery chart uses the inverter's Modbus reading for available discharge energy; battery energy is not calculated from state of charge.

The Docker image starts the minute collector, live collector, and dashboard. Publish port `8080` when running the container, for example `docker run -p 8080:8080 -v solar-config:/config solar-model`.
