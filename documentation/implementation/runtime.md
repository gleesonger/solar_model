# Runtime, configuration, and object ownership

Sources: [solar_model/runtime/supervisor.py](../../solar_model/runtime/supervisor.py), [solar_model/runtime/collector.py](../../solar_model/runtime/collector.py), [solar_model/dashboard/app.py](../../solar_model/dashboard/app.py), [solar_model/config.py](../../solar_model/config.py), [solar_model/time_utils.py](../../solar_model/time_utils.py).

Root files are compatibility launchers. Installable console commands and module entry points call the same package functions. The supervisor and Control subprocesses inherit configuration-directory working paths and receive the package location in `PYTHONPATH`; resources resolve inside the package.

## 1. Managed process hierarchy

```text
solar_model.runtime.supervisor.main()
  start_processes()                       nested supervisor helper
    load_config()
    open_database(config.database.path)
    database.close()
    subprocess.Popen(python -m solar_model.runtime.collector)
    subprocess.Popen(python -m solar_model.dashboard.app)
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
  expand projection_resolution_mins repetitions (e.g. 15*24)
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
    "forecast": {"solar": {"arrays": [{"panel_id": 1, "name": "East", ...}], ...}, "load": {...}},
    "tariffs": {"periods": [{"effective_from": "2000-01-01",
                             "import": [...], "export": [...]}]},
    ...,
}

# Corresponding typed relationships
config.forecast.solar.arrays                 # tuple[SolarArrayConfig, ...]
config.tariffs.periods                 # tuple[TariffPeriod, ...]
config.tariffs.periods[0].import_       # tuple[TariffRule, ...]
config.dashboard.actuals_to_forecast   # dict[str, str], e.g. {'pv1': 'East'}
config.actuals_to_forecast_panel_ids   # resolves names to IDs for queries/charts
```

| `Config` branch | Important nested types and consumers |
| --- | --- |
| `logging`, `database` | `LoggingConfig.level`, `DatabaseConfig.path`; process setup/readers |
| `actuals` | `ModbusConfig`, `DataRetrivalScheduleConfig`; full/live collection |
| `forecast` | `ForecastConfig`, tuple of `SolarArrayConfig`; API requests, panel labels, model/UI selection |
| `tariffs` | `TariffsConfig` → `TariffPeriod` → `TariffRule`; collection, planning, recalculation |
| `energy_plan` | `EnergyPlanConfig`, scalar protection preference, limit-rule tuples; offline planner |
| `additional_device_info` | Charge/discharge efficiencies; overrides planner metadata inputs |
| `dashboard` | Host/port and PV-string mapping; NiceGUI and array allocation |

`SolarArrayConfig` contains ID, label, coordinates, declination, azimuth, and `peak_kw`. `TariffRule` contains weekday tuple, start/end text, numeric `rate`, optional band name; its minute properties parse the text. Scheduled planning limits reuse this type but assign a different unit to `rate`.

Validation checks complete weekday/minute tariff coverage, effective-date ordering, positive timing/history settings, projection divisibility, weights, efficiencies, mappings, URL shape and connection values. See [configuration overview](../overview/configuration.md) for rules and gaps; this guide does not duplicate every range check.

`ForecastConfig` contains `solar: SolarForecastConfig` and `load: LoadForecastConfig`. Solar owns provider scheduling/connection settings, arrays and day-ahead/intraday calibration. Load owns house-load calibration. `config.forecast.models` assembles the unchanged `ForecastModelsConfig` bundle passed to calculation/services APIs. Validation checks model-specific constraints while loading. Omitted calibration blocks within the nested layout retain defaults; the old flat YAML layout must be updated. No forecast model reads YAML per observation.

## 3. Collector-owned objects

`solar_model.runtime.collector.main()` keeps these objects for its entire loop:

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
solar_model.dashboard.app.main()
  load_config(), configure_logging(), open_database()/close()
  LivePowerCollector(...)
  DashboardSnapshotStore(config, live_collector, full_interval)
  register app startup/shutdown callbacks
  ui.page('/') -> render_dashboard_page()
  ui.run(..., favicon=SVG file path, reload=False)

app startup callbacks (scheduled independently)
  start_snapshot_worker(): await snapshots.refresh(); start snapshots.run_forever()
  ensure_planning_worker(): await one shared asynchronous warm-up task
    await run.cpu_bound(services.planning.warm_planning_worker)
    import planning dependencies, initialise highspy.Highs(), return worker PID
    log worker ready/startup seconds; log failures without aborting dashboard startup

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

At startup, `main_dashboard` passes the source-relative `solar_model/dashboard/assets/solar_power.svg` path to `ui.run(favicon=...)`. NiceGUI references `/favicon.ico` in the page icon link and serves the SVG file through that route. The SVG is not embedded in the page. Deploy the valid asset, restart the dashboard and reload the page; the fixed favicon URL may require browser cache refresh after image changes.

`apply_snapshot` uses `client.safe_invoke` to update that client's UI context. Reconnect replaces the subscription and reapplies the current snapshot. Disconnect unsubscribes and reevaluates whether live polling should pause. The server snapshot worker continues refreshing without a connected browser; live Modbus polling does not.

## 5. Paths, timestamps, and utilities

`solar_model.utils.table_to_object_list(class_type, *args)` returns `[class_type(*row) for row in args]`. Each row is an iterable of positional constructor arguments, such as a tuple, list or generator. Rows are consumed once in their iteration order; callers should supply ordered iterables. Row order is preserved, omitted trailing arguments use constructor defaults, and no rows returns an empty list. Constructor errors propagate to the caller. The generic return type is `list[T]` for a supplied `type[T]`.

```python
columns = table_to_object_list(Column,
    # key       label     decimals
    ("solar",   "Solar",   2),
    ("load",    "Load",    2),
)
```

- Config and relative database paths resolve against **process working directory**. Register maps and assets resolve inside the installed package (`package_path()` for shared resource lookup); subprocesses run package modules.
- Docker stores source under `/app` but runs from `/config`; its entrypoint creates YAML only if absent. It does not copy updated settings over an existing volume.
- `timestamps()` returns `(UTC ISO seconds, configured-local ISO seconds)` from one instant. Database decorators later convert measurement timestamps to epochs.
- `heavy_work(name)` wraps synchronous operations, logging duration and re-raising failures; it does not schedule threads or transactions.
- `configure_logging()` changes root logging and suppresses pymodbus protocol chatter. Read-query timing is attached to cached database engines at DEBUG level.
- `try_get()` catches only the explicitly supplied exception type. It is an error-handling helper, not a global fallback policy.

Next: [collection and ingestion](collection.md). The [dashboard state guide](dashboard-ui.md) explains the worker/thread/browser boundaries in more detail.
