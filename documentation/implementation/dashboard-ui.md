# Dashboard live state, caching, and presentation

Sources: [main_dashboard.py](../../main_dashboard.py), [layout.py](../../dashboard/layout.py), [snapshot.py](../../dashboard/snapshot.py), [live.py](../../dashboard/live.py), [recent.py](../../dashboard/recent.py), [anlaysis.py](../../dashboard/anlaysis.py), [models.py](../../dashboard/models.py).

Read [dashboard-data.md](dashboard-data.md) for database queries and dataset columns, and the [styling guide](../overview/styling.md) for visual conventions. This guide covers ownership and the final transformations into user-visible values.

## 1. Shared cache to per-client elements

```text
DashboardSnapshotStore.refresh()
  load shared Recent tuple and device list in worker threads
  swap _snapshot under lock; increment version
  copy listener callbacks under lock
  notify each callback outside lock

browser apply_snapshot(snapshot)
  client.safe_invoke(update_client)
    initial render -> DashboardLayout.render_dashboard_layout()
    subsequent -> DashboardLayout.apply_snapshot()
      RecentTab.apply_loaded_data(snapshot.recent_data)
      SystemInfoTab.apply_loaded_data(snapshot.device_information)
```

The store's lock protects snapshot/listener references. It does not lock database readers or freeze pandas contents. Failed refresh leaves the previous snapshot installed. `run_forever()` waits the configured interval after each iteration, so refresh cost adds to cadence.

`DashboardLayout` owns mutable `DashboardState` and `DashboardElements`. State tracks active tab, range/grouping/aggregation, and zoom. Elements holds references to NiceGUI labels/tables/charts plus applied live timestamp. Those references belong to a browser page, not the shared snapshot.

`AnalysisTab` is created with the layout, but charts are built lazily on first tab activation. Its local selected range persists in `DashboardState`; shared snapshot application updates only Recent/System Info. The old `HourlyTab` and related element fields remain but are not mounted.

### Registering a new standard Analysis chart

`DashboardElements.historical_charts` holds the fixed `HistoricalCharts` dataclass from `models.py`. Its current fields are `energy`, `power`, `battery`, `array_energy`, and `money`, each a `ChartDataDisplay`. There is no automatic discovery of additional rendered charts.

Adding a standard chart requires all three registrations:

1. Add its `ChartDataDisplay` field to `HistoricalCharts`.
2. Construct the display and include it in `charts.render_historical_charts()`'s returned record. Pass a `chart_options` callback that uses the updated DataFrame rather than capturing initial data.
3. Explicitly update the new field in `AnalysisTab._update_historical_charts()`, using the appropriate returned dataset.

Both desktop and mobile Apply controls run through `_render_controls()`'s `apply_range()` and this update method. Rendering a chart alone leaves it outside subsequent refreshes; registering it preserves updated tables and clipboard/CSV data as well as chart options.

## 2. Live collector call and state hierarchy

```text
browser connect/disconnect
  LivePowerCollector.refresh_browser_activity()
    inspect NiceGUI Client.instances socket state
    set/clear _active_event
    start() thread if needed

thread _run()
  wait while inactive; close existing client
  _connect() if needed
  _collect_once(client)
    read_registers(filtered blocks)
    timestamps()
    _power_values(readings, local_timestamp)
      read local-day grid baseline if day changed
      _add_live_pv_array_values()
    replace _latest under lock
  wait live interval; retry

browser timer -> layout.refresh_live_power()
  RecentTab.refresh_live_power()
    live_collector.snapshot() -> copy timestamp/value dictionary
    if timestamp changed: update cells and indicator
    publish live timestamp to warning metadata
```

`_stop_event` ends the thread; `_active_event` controls browser-driven polling; `_lock` protects newest reading/activity access. Client connection and daily integration state are collector-thread-owned. Failed reads retain `_latest`; retry replaces the connection. `snapshot()` returns a new `LivePowerData` containing a copied values dictionary.

## 3. LivePowerData structure

```python
LivePowerData(
    collected_at_utc="2026-09-15T09:00:00+00:00",
    collected_at_local="2026-09-15T10:00:00+01:00",
    values={
        "solar": 3.0, "load": 1.0, "battery": -1.5,
        "grid": -0.5, "grid_import": 0.0, "grid_export": 0.5,
        "inverter": 2.5,
        "battery_available_energy_kwh": 8.0, "battery_soc_percent": 50.0,
        "today_solar": 6.0, "today_load": 4.0,
        "today_battery_charge": 2.0, "today_battery_discharge": 1.0,
        "today_grid_import": 0.5, "today_grid_export": 2.0,
        "pv1": 2.0, "pv2": 1.0,
        "today_pv1": 4.0, "today_pv2": 2.0,
    },
)
```

Example values are synthetic; signed battery power is passed through without interpreting the sign here. Keys for mapped strings vary by configuration. Values are physical floats or `None`; initial timestamps are `None`, initial values missing. `_empty_values()` seeds summary/daily/battery/string keys; actual power mapping adds `grid` and `inverter`.

| Values group | Source / meaning |
| --- | --- |
| `solar/load/battery/grid/inverter` | Decoded instantaneous plant/inverter kW |
| `grid_import/grid_export` | `max(grid,0)` and `max(-grid,0)`, non-negative kW |
| Battery availability/SOC | Direct plant discharge-energy and SOC registers |
| Daily solar/load/charge/discharge | Direct daily counters; no integration in the tab |
| Daily grid import/export | Lifetime counters minus day baseline, clipped ≥0 |
| `pvN`, `today_pvN` | Allocated plant values using measured string proportions |

## 4. Daily baseline and allocation state

Grid baseline state is `_grid_energy_day` plus `_grid_energy_start: dict[str,float]`. On local-day change, `load_grid_energy_day_start()` queries the first persisted electrical row in that configured-local day, projecting lifetime grid import/export meters. No row means the first live value becomes baseline through `setdefault`. Daily grid total therefore depends on startup/day baseline availability, not an independently read device daily-grid meter.

PV integration state consists of:

- `_pv_energy_day`: local date marker.
- `_pv_energy_base`: persisted per-string interval-energy totals for the day, keyed by numeric string ID.
- `_pv_energy_live`: subsequent trapezoidal integrals, keyed by string ID.
- `_previous_pv_power` and `_previous_pv_timestamp`: integration endpoints.
- `_last_pv_shares`: most recent positive total-power proportions keyed by `pvN`.

At day change, the base is loaded from `sigenstor`, live integrals reset, and previous/shares clear. Grid/PV baselines and date markers are published only after successful computation. A failed baseline read leaves the prior day/state intact and is retried on the next poll; failed later string processing does not partially advance integration or double-count a retry. Each live reading computes `max(V*A,0)/1000`. If total raw string power is positive, proportions refresh; otherwise prior positive proportions are retained. Displayed string kW is plant solar × share. Displayed string daily kWh is plant daily solar × `(persisted integral + live integral)` proportion. No share yields missing unless plant total is explicitly zero.

These values are in-memory allocations and never write to SQLite. Polling pauses/failures can leave elapsed gaps between integration endpoints; no separate continuous background energy estimator runs while inactive.

## 5. Recent tuple to formatted table rows

`RecentTab` builds `_summary_latest_keys` from standard rows plus orientation labels such as `" - East" -> "pv1"`. `data.summary_rows(day,latest,mapping)` creates:

```python
{
    "metric": "Solar",
    "latest_key": "solar",
    "is_solar_orientation": False,
    "latest": "3.0",
    "today": "6.0",
    "latest_forecast": "18.0",
    "forecast": "17.5",       # field name retained; BoD-adjusted meaning
    "forecast_raw": "16.8",
}
```

`latest_key` and orientation flag are metadata for updates/style; visible fields are formatted strings. Missing numeric values become empty strings; energy/power format to one decimal, currency summaries to two decimals. Per-orientation forecast cells are not filled just because panel forecast columns exist; the summary forecast maps currently contain site solar totals only.

Initial/full update builds rows from the DB-derived day frame, then `_update_live_table_rows()` overwrites latest/Today from live state. `_today_values` is keyed by display label. `_apply_live_today_meters()` replaces available solar/load/import/export/string daily meters and computes Battery = daily charge − discharge. The tab does not integrate power itself. Day-crossing detection resets its held daily values.

Daily/monthly DataFrames use `energy_summary_rows()` to format the nine energy/economics fields and period labels. System Info binds the latest metadata list directly. Full updates replace row lists/label text while retaining the tables and tabs.

## 6. Analysis data to ECharts and reusable controls

```text
AnalysisTab selected_range / apply_range
  validate dates/count/group/aggregation
  analysis_range_bounds()
  run.io_bound(load_analysis_range)
  _update_historical_charts()
    ChartDataDisplay.update(frame)

custom builder refresh_preview
  load_custom_chart_preview() in worker
  retain preview dataset + visible metric/split sets
  render_dimension_legend()
  custom_chart_preview_options()
  update existing EChart
```

`charts.py` turns frames into ECharts options: category labels, named series, units/axes, tooltip, legend, colours, decals, and zoom. `chart_values()` converts pandas missing values to `None`, rounds to one decimal, and normalises negative rounded zero. This formatting is later than full-precision aggregation.

Typical browser options have `xAxis.data: list[str]` and `series: list[{name,type,data,itemStyle,...}]`. Custom series carry the metric/split identity in the preview; presentation translates it into display names and stable styles, applying visibility filtering after style allocation.

`ChartDataDisplay` owns the DataFrame, title/hint callbacks, chart-options callback, chart element, table element, and show-table flag. It supports tab-separated clipboard, CSV download, and an optional Total row whose fields sum or mean according to configured aggregation. Normal updates retain browser legend selection by removing incoming `legend.selected`; explicit `refresh_chart()` uses `notMerge=True`.

Analysis power and battery/SOC tables omit Total rows because summing levels or percentages is misleading. Missing aggregate values render blank. Energy/array/money tables retain their configured totals. The custom builder uses responsive controls and a stacked plot/legend on mobile; the persistent warning sits below modal dialogs so Close remains usable.

Single-day telemetry already contains minute points, so its zoom is client-side. Longer-range telemetry remains hourly. Chart/CSV values and Analysis full-data downloads follow different export routes; see below.

## 7. Export bytes and operator actions

`download_data_dialog.prepare_download()` receives bounds/timestep/forecast flag/format, calls `analysis_export_dataframe`, and returns `(content: bytes, filename: str, media_type: str)`. CSV serialises directly. Excel removes timezone metadata while retaining displayed local time, then passes the whole pandas DataFrame to `xlsxturbo.df_to_xlsx` in constant-memory mode into `BytesIO`. XlsxTurbo uses the Rust `rust_xlsxwriter` library; project code performs no row/cell writing or formatting loops. Missing cells remain blank, infinities become text, datetimes retain date formatting, and headers remain formatted. Install `xlsxturbo>=1.11,<2` from requirements. Sheet-limit violations give a validation error. Logs separate data preparation from file generation.

Chart-specific CSV/clipboard serialise the component's current chart DataFrame. They do not query all underlying database fields. Full Analysis downloads aggregate the broader declared database field set directly in SQLite as described in [dashboard-data.md](dashboard-data.md).

`ControlTab` turns date inputs into a subprocess argv list, runs recalculation in `run.io_bound`, and after success requests a supervisor restart. It confirms writes/restarts with local dialogs. Download failures log the underlying exception, restore the form, and show the shared error notification so the user can correct the inputs or retry.

## 8. Python-to-browser freshness contract

`DashboardWarnings` creates hidden metadata with `data-last-full-update`, `data-last-live-update`, full/live thresholds, and heartbeat timeout. It sends `window.dashboardHeartbeat?.()` every five seconds. Full freshness uses last persisted collection time; live freshness uses last successful live collection time, not UI render time.

[warnings.js](../../dashboard/assets/warnings.js) reads DOM dataset strings, parses timestamp ages, checks navigator/socket/heartbeat, and owns the fixed warning banner. Offline/socket/resume invalidates the heartbeat; a fresh heartbeat is required to clear that condition. Stale timestamps keep the banner visible even when connection is healthy.

[live_updates.js](../../dashboard/assets/live_updates.js) owns only indicator visibility and its three-second idle timer. It does not assess data freshness. Action notifications use shared Python helpers; persistent warning policy is not duplicated in tab callbacks.
