# Runtime, configuration, and object ownership

Sources: [docker_start.py](../../docker_start.py), [main_data_collection.py](../../main_data_collection.py), [main_dashboard.py](../../main_dashboard.py), [config.py](../../config.py), [common.py](../../common.py).

## 1. Managed process hierarchy

```text
docker_start.main()
  start_processes()                       nested supervisor helper
    load_config()
    open_database(config.database.path)
    database.close()
    subprocess.Popen(main_data_collection.py)
    subprocess.Popen(main_dashboard.py)
  supervision loop
    restart marker exists -> stop_processes() -> start_processes()
    child exits -> record exit code -> stop remaining children
  shutdown -> stop_processes() -> remove restart marker
```

The supervisor does not pass a `Config` object to children. Each process rereads the same working-directory `config.yaml`. Database initialisation occurs first to reduce concurrent startup migration work, but each child also calls `open_database()`.

Supervisor state is a list of `Popen` objects, a boolean shutdown flag, and a temporary restart-file `Path`. The environment variable `SOLAR_MODEL_RESTART_FILE` passes that path to both children. Dashboard Control touches it; the supervisor detects it at approximately 0.25-second intervals. There is no persistent IPC message queue or configuration hot reload.

Shutdown terminates children, waits up to ten seconds per child, then kills survivors. A child crash stops the managed application; automatic restart after a crash requires an external service/container policy.

If either child launch fails, `start_processes()` terminates and reaps any children already launched, then propagates the original failure. This applies during both initial start and requested restart.

## 2. YAML to typed configuration

```text
load_config(path='config.yaml')
  yaml.safe_load(file) -> Python mapping/lists/scalars
  normalise minimum_battery_kwh wrapper if supplied
  rename each tariff period's import key to import_
  dacite.from_dict(Config, cast=[tuple])
  validate_config(config)
  return Config
```

The intermediate YAML object is mutable. The normalisation steps alter it before constructing frozen dataclasses. Unknown keys are ignored because dacite strict mode is not enabled; this is why descriptive `name`/`comment` keys in YAML do not become runtime fields.

```python
# Abbreviated raw YAML-derived mapping
{
    "timezone": "Europe/Dublin",
    "forecast": {"arrays": [{"panel_id": 1, "name": "East", ...}], ...},
    "tariffs": {"periods": [{"effective_from": "2000-01-01",
                             "import": [...], "export": [...]}]},
    ...,
}

# Corresponding typed relationships
config.forecast.arrays                 # tuple[SolarArrayConfig, ...]
config.tariffs.periods                 # tuple[TariffPeriod, ...]
config.tariffs.periods[0].import_       # tuple[TariffRule, ...]
config.dashboard.actuals_to_forecast   # dict[str, int], e.g. {'pv1': 1}
```

| `Config` branch | Important nested types and consumers |
| --- | --- |
| `logging`, `database` | `LoggingConfig.level`, `DatabaseConfig.path`; process setup/readers |
| `actuals` | `ModbusConfig`, `DataRetrivalScheduleConfig`; full/live collection |
| `forecast` | `ForecastConfig`, tuple of `SolarArrayConfig`; API requests, panel labels, model/UI selection |
| `tariffs` | `TariffsConfig` → `TariffPeriod` → `TariffRule`; collection, planning, recalculation |
| `energy_plan` | `EnergyPlanConfig`, `OptimisationWeightsConfig`, limit-rule tuples; offline planner |
| `additional_device_info` | Charge/discharge efficiencies; overrides planner metadata inputs |
| `dashboard` | Host/port and PV-string mapping; NiceGUI and array allocation |

`SolarArrayConfig` contains ID, label, coordinates, declination, azimuth, and `peak_kw`. `TariffRule` contains weekday tuple, start/end text, numeric `rate`, optional band name; its minute properties parse the text. Scheduled planning limits reuse this type but assign a different unit to `rate`.

Validation checks complete weekday/minute tariff coverage, effective-date ordering, positive timing/history settings, projection divisibility, weights, efficiencies, mappings, URL shape and connection values. See [configuration overview](../overview/configuration.md) for rules and gaps; this guide does not duplicate every range check.

## 3. Collector-owned objects

`main_data_collection.main()` keeps these objects for its entire loop:

| Object | Shape / lifetime |
| --- | --- |
| `config` | One `Config`, loaded once per process start |
| `registers`, `device_registers` | Two lists of `RegisterBlock`; compiled from source-relative maps |
| `database` | `SolarDatabase`, holding Engine and session factory |
| `client` | Connected `ModbusTcpClient` or `None`; replaced after failures |
| `previous` | Mutable mixed-value dictionary of prior cumulative values, PV powers, and timestamp |
| `session` | Reused HTTP `requests.Session` |
| Forecast eligibility | Persisted `database_metadata.forecast_last_attempt_utc`, with latest saved collection as fallback |

The full interval aligns to wall-clock multiples through `sleep_until_next_interval`. Each cycle calls the forecast collector, whose database due check uses UTC elapsed seconds and survives process restart. No in-memory forecast timer remains. The scheduler is sequential; a forecast scan occurs only when the configured interval has elapsed since the latest attempt/saved collection.

## 4. Dashboard startup and browser hierarchy

```text
main_dashboard.main()
  load_config(), configure_logging(), open_database()/close()
  LivePowerCollector(...)
  DashboardSnapshotStore(config, live_collector, full_interval)
  register app startup/shutdown callbacks
  ui.page('/') -> render_dashboard_page()
  ui.run(..., favicon=SVG contents, reload=False)

app startup
  await snapshots.refresh()
  background_tasks.create(snapshots.run_forever())

each browser page
  DashboardLayout(config, live_collector)
    DashboardState + DashboardElements
    DashboardWarnings
    RecentTab / AnalysisTab / SystemInfoTab / ControlTab
  render cached snapshot, or loading state
  subscribe apply_snapshot callback
  register connect/disconnect callbacks
  ui.timer(live_interval, layout.refresh_live_power)
```

One server-wide live collector and snapshot store serve all pages. `DashboardState`, `DashboardElements`, tab instances, warnings metadata and subscriptions belong to individual pages. The persisted collector runs in another process and does not share these Python objects.

At startup, `main_dashboard` reads `dashboard/assets/solar_power.svg` as UTF-8 and passes its contents to `ui.run(favicon=...)`. NiceGUI emits an SVG data URL in the page icon link, so changed icon contents have a different URL instead of sharing the fixed `/favicon.ico` file URL. The SVG is read at startup; restart and reload to apply edits. Source-relative resolution is retained.

`apply_snapshot` uses `client.safe_invoke` to update that client's UI context. Reconnect replaces the subscription and reapplies the current snapshot. Disconnect unsubscribes and reevaluates whether live polling should pause. The server snapshot worker continues refreshing without a connected browser; live Modbus polling does not.

## 5. Paths, timestamps, and utilities

- Config and relative database paths resolve against **process working directory**. `source_path()` resolves relative register-map/favicon/script paths against source.
- Docker stores source under `/app` but runs from `/config`; its entrypoint creates YAML only if absent. It does not copy updated settings over an existing volume.
- `timestamps()` returns `(UTC ISO seconds, configured-local ISO seconds)` from one instant. Database decorators later convert measurement timestamps to epochs.
- `heavy_work(name)` wraps synchronous operations, logging duration and re-raising failures; it does not schedule threads or transactions.
- `configure_logging()` changes root logging and suppresses pymodbus protocol chatter. Read-query timing is attached to cached database engines at DEBUG level.
- `try_get()` catches only the explicitly supplied exception type. It is an error-handling helper, not a global fallback policy.

Next: [collection and ingestion](collection.md). The [dashboard state guide](dashboard-ui.md) explains the worker/thread/browser boundaries in more detail.
