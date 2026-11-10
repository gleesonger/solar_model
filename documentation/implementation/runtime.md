# Runtime, configuration, and object ownership

Sources: [solar_model/runtime/supervisor.py](../../solar_model/runtime/supervisor.py), [solar_model/runtime/collector.py](../../solar_model/runtime/collector.py), [solar_model/dashboard/app.py](../../solar_model/dashboard/app.py), [solar_model/config.py](../../solar_model/config.py), [solar_model/time_utils.py](../../solar_model/time_utils.py).

Root files are compatibility launchers. Installable console commands and module entry points call the same package functions. The supervisor and Control subprocesses inherit configuration-directory working paths and receive the package location in `PYTHONPATH`; resources resolve inside the package.

## 1. Managed process hierarchy

The supervisor migrates storage, then starts `runtime.collector`, `runtime.planner` and `dashboard.app`. Each is an `OwnedProcess`: a separate POSIX session/process group or a Windows kill-on-close Job Object. Startup launch failure cleans all children already started. Once running, a service exit reaps its descendants and restarts only that service after exponential 1?30 second backoff; a service surviving 60 seconds resets its failure count. Collector and planner continue through dashboard failure. A Control restart marker stops/restarts all three and reloads configuration/storage. Stop waits are bounded; Windows Job closure forcibly stops the owned tree.

The headless `runtime.planner.PlannerService` checks input markers/settings once per second, keeps at most one in-flight calculation, coalesces readings arriving during work and applies the existing cadence tolerance. At startup it restores previous publication/cadence metadata, establishes the latest-reading baseline and waits for a newer reading. Calculation failures retry on a later new reading. Its owned calculation worker captures the input cutoff inside the worker, enforces the configured time limit, and saves before returning an ID. Expired/crashed workers are reaped and recreated on later submissions. `database_metadata.planning_service_status` carries PID, worker PID, heartbeat, state, last error and last successful publication. Status writes occur on state changes and every five seconds.

The dashboard polls saved plan IDs/status, not electrical markers. Its private demonstration worker is created only on demand and admits one request globally; further requests receive a busy message. It cannot delay the independent scheduled worker. See [saved-plan contracts](plan-history.md).

## 2. YAML to typed configuration

```text
load_config(path='config.yaml')
  yaml.safe_load(file) -> Python mapping/lists/scalars
  config_from_mapping(...) shares normalisation/validation with the UI editor
  validate scenarios dimensions contain exactly low/med/high multiplier/probability entries
  expand optimiser.mix_integer_linear_prog.projection_resolution_mins repetitions (e.g. 15*24)
  normalise minimum_battery_kwh wrapper if supplied
  rename each tariff period's import key to import_
  dacite.from_dict(Config, cast=[tuple])
  validate_config(config)
  return Config
```

The intermediate YAML object is mutable. The normalisation steps alter it before constructing frozen dataclasses. Unknown keys are ignored because dacite strict mode is not enabled; this is why descriptive `name`/`comment` keys in YAML do not become runtime fields.

`EnergyPlanConfig.plan_rerun_frequency_mins` has no constructor default. Both startup file loading and strict editor parsing reject an omitted field before a scheduler or plan starts. It must also be finite and positive. Other optional YAML fields are listed in the [configuration defaults catalogue](../overview/configuration.md#optional-fields-and-defaults).

Required `Config.energy_plan.scenarios: ScenariosConfig` contains `solar` and `house_load` as frozen `ScenarioDimensionConfig` objects, each with low/med/high `ScenarioLevelConfig(multiplier, probability)` entries. `items()` returns the fixed low/med/high order. Multipliers are finite, non-negative and nondecreasing; probabilities are finite in [0,1] and must total 97%-103% per dimension, then are normalised to sum to one during construction. Scenario mappings reject extra/missing levels and entry fields. Planning and dashboard share these settings; joint probabilities assume independence.

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
| `energy_plan` | `EnergyPlanConfig`, scalar protection preference, limit-rule tuples and nested `OptimiserConfig` with `MixedIntegerLinearProgrammingConfig` and `DynamicProgrammingConfig` / `DPRefinementConfig` stages; offline planners |
| `additional_device_info` | Charge/discharge efficiencies; overrides planner metadata inputs |
| `dashboard` | Host/port and PV-string mapping; NiceGUI and array allocation |

`SolarArrayConfig` contains ID, label, coordinates, declination, azimuth, and `peak_kw`. `TariffRule` contains weekday tuple, start/end text, numeric `rate`, optional band name; its minute properties parse the text. Scheduled planning limits reuse this type but assign a different unit to `rate`.

Validation checks complete weekday/minute tariff coverage and disallows overlapping battery-limit rules; battery schedules may omit times and use their defaults. It also checks effective-date ordering, positive timing/history settings, projection divisibility, weights, efficiencies, mappings, URL shape and connection values. See [configuration overview](../overview/configuration.md) for rules and gaps; this guide does not duplicate every range check.

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
  PlanStore(config, config_path)
  register app startup/shutdown callbacks
  ui.page('/') -> render_dashboard_page()
  ui.run(..., favicon=SVG file path, reload=False)

app startup callbacks
  start_snapshot_worker(): start snapshots.run_forever(), awaiting subscribers
  start_plan_worker(): start plans.run_forever(), observing saved IDs/status
  no optimiser worker warm-up or shared calculation in the dashboard

each browser page
  DashboardLayout(config, live_collector)
    DashboardState + DashboardElements
    DashboardWarnings
    RecentTab / AnalysisTab / PlanTab (including saved selection) / SystemInfoTab / ControlTab
  render cached snapshot, or loading state
  subscribe apply_snapshot callback
  subscribe plan_tab.apply_shared_plan through client.safe_invoke
  register connect/disconnect callbacks
  ui.timer(live_interval, layout.refresh_live_power)
```

One server-wide live collector, Recent snapshot store and saved-plan cache serve all pages. `DashboardState`, `DashboardElements`, tab instances, warnings metadata and subscriptions belong to individual pages. The persisted collector runs in another process; the independent planning service observes its committed electrical timestamps through SQLite. The dashboard observes saved plan IDs through SQLite. Only `energy_plan` supports hot reload from the startup file; other settings retain restart semantics. Plan scheduling and browser demonstration behaviour are detailed in [dashboard ownership](dashboard-ui.md#plan-tab-ownership-and-refresh).

At startup, `main_dashboard` passes the source-relative `solar_model/dashboard/assets/solar_power.svg` path to `ui.run(favicon=...)`. NiceGUI references `/favicon.ico` in the page icon link and serves the SVG file through that route. The SVG is not embedded in the page. Deploy the valid asset, restart the dashboard and reload the page; the fixed favicon URL may require browser cache refresh after image changes.

`apply_snapshot` uses `client.safe_invoke` to update that client's UI context. Reconnect replaces the subscription and reapplies the current snapshot. Disconnect unsubscribes and reevaluates whether live polling should pause. Recent snapshots and live Modbus polling pause without connected browsers. Background plan change detection continues independently.

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

The UI editor uses parse_energy_plan_yaml(config, text) to construct a strict replacement EnergyPlanConfig, then validate it within a copied Config. energy_plan_yaml explicitly maps the EnergyPlanConfig schema to the file layout through EnergyPlanYamlDumper: compact scalar lists, inline scenario/refinement mappings, quoted times and separated sections. collapse_projection_resolutions encodes consecutive equal timestep runs; collapse_scheduled_rules groups identical time/rate/name combinations into weekday lists without altering time windows. Rule comment metadata is discarded during shared normalisation. Round-trip checks cover file YAML to settings to formatted YAML to equivalent settings, plus stable regenerated text. save_energy_plan_yaml validates against both the running config and latest disk contents, writes a temporary sibling file and atomically replaces the target. Only the energy_plan source section is replaced. Startup config_path is passed to the shared PlanStore and through DashboardLayout to PlanTab; ordinary startup defaults to config.yaml. Successful editor saves update the store immediately, and external file edits are detected by the background check. Only energy_plan is hot-reloaded.
