# Dashboard live state, caching, and presentation

Sources: [solar_model/dashboard/app.py](../../solar_model/dashboard/app.py), [layout.py](../../solar_model/dashboard/layout.py), [snapshot.py](../../solar_model/dashboard/snapshot.py), [live.py](../../solar_model/dashboard/live.py), [recent.py](../../solar_model/dashboard/tabs/recent.py), [analysis.py](../../solar_model/dashboard/tabs/analysis.py), [state.py](../../solar_model/dashboard/state.py).

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

`AnalysisTab` is created with the layout, but charts are built lazily on first tab activation. Its local selected range persists in `DashboardState`; shared snapshot application updates only Recent/System Info. The unmounted Hourly tab and its unused UI state/element fields were removed.

### Registering a new standard Analysis chart

`DashboardElements.historical_charts` holds the fixed `HistoricalCharts` dataclass from `state.py`. Its current fields are `energy`, `power`, `battery`, `array_energy`, and `money`, each a `ChartDataDisplay`. There is no automatic discovery of additional rendered charts.

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

`latest_key` and orientation flag are metadata for updates/style; visible fields are formatted strings. Missing numeric values become empty strings; energy/power format to one decimal, currency summaries to two decimals. Per-orientation forecast cells are not filled just because panel forecast columns exist; the solar forecast maps contain site solar totals only. House Load has its own BoD total from the saved daily house profile, with Latest/Raw Forecast left blank.

Initial/full update builds rows from the DB-derived day frame, then `_update_live_table_rows()` overwrites latest/Today from live state. `_today_values` is keyed by display label. `_apply_live_today_meters()` replaces available solar/load/import/export/string daily meters and computes Battery = daily charge − discharge. House Load Today is seeded from the current stored-data frame on full refresh and is retained during faster live polls; its Latest uses the measured load-power reading. The separate Load row is excluded from RecentTab's live-table mapping on initial render and full refresh. The tab does not integrate power itself. Day-crossing detection resets its held daily values, including House Load until the next full snapshot.

Daily/monthly DataFrames use `energy_summary_rows()` to format the actual energy/economics fields, including House Load, and period labels. They do not contain a house-forecast column. System Info binds the latest metadata list directly. Full updates replace row lists/label text while retaining the tables and tabs.

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

`ChartDataDisplay` owns the DataFrame, title/hint callbacks, chart-options callback, chart element, table element, and show-table flag. It supports tab-separated clipboard, CSV download, and an optional Total row whose fields sum or mean according to configured aggregation. Normal updates preserve browser selections for previously seen series, apply default selections only to newly introduced series, and replace the series list so removed series disappear when grouping changes; explicit `refresh_chart()` uses `notMerge=True`.

Callers can enable `show_series_selection=True` to add one series-selection button to the shared header toolbar alongside optional caller controls, copy, download and table buttons. Analysis Energy, Power and Costs enable this option. Initially labelled **Default**, successive clicks apply **None → All → Default** and show the applied state. Default restores the chart's original per-series visibility, including initially hidden energy forecasts/counter Load and cost comparators. Default selections are retained separately from mutable browser selections; defaults for newly introduced series are remembered on data updates. Each action updates the current series' legend selections through ECharts `setOption`, preserving data zoom and data/table/export contents. Individual legend selections remain usable; normal data updates preserve those selections. Cost tariff splitting reads the browser's current legend state when transferring import visibility between combined and split series.

Analysis power and battery/SOC tables omit Total rows because summing levels or percentages is misleading. Missing aggregate values render blank. Energy/array/money tables retain their configured totals. The custom builder uses responsive controls and a stacked plot/legend on mobile; the persistent warning sits below modal dialogs so Close remains usable.

Power chart series and table labels override `load` to **House Load**, since the value is measured household kW. The shared energy label for `load` remains **Load**, representing device-counter kWh. DataFrame/CSV field names are unchanged. Recent Live/Today hides the separate Load row; the Past tables retain both device-counter Load and integrated House Load energy.

Single-day telemetry already contains minute points, so its zoom is client-side. Longer-range telemetry remains hourly. Chart/CSV values and Analysis full-data downloads follow different export routes; see below.

Only the hourly Energy frame includes `house_load_forecast`. `energy_chart_options()` adds a House Load (Forecast) bar with `forecast_item_style(LOAD_COLOR)` and defaults its legend selection to false. Switching from a non-hourly grouping applies that default on first appearance; later updates preserve the user's choice. Recent Live/Today reads the saved day profile into House Load's BoD Forecast cell. The two Past tables omit house forecasts.

## 7. Export bytes and operator actions

`download_data_dialog.prepare_download()` receives bounds/timestep/forecast flag/format, calls `analysis_export_dataframe`, and returns `(content: bytes, filename: str, media_type: str)`. CSV serialises directly. Excel removes timezone metadata while retaining displayed local time, then passes the whole pandas DataFrame to `xlsxturbo.df_to_xlsx` in constant-memory mode into `BytesIO`. XlsxTurbo uses the Rust `rust_xlsxwriter` library; project code performs no row/cell writing or formatting loops. Missing cells remain blank, infinities become text, datetimes retain date formatting, and headers remain formatted. Install `xlsxturbo>=1.11,<2` from requirements. Sheet-limit violations give a validation error. Logs separate data preparation from file generation.

Chart-specific CSV/clipboard serialise the component's current chart DataFrame. They do not query all underlying database fields. Full Analysis downloads aggregate the broader declared database field set directly in SQLite as described in [dashboard-data.md](dashboard-data.md).

`ControlTab` turns date inputs into a subprocess argv list, runs recalculation in `run.io_bound`, and after success requests a supervisor restart. It confirms writes/restarts with local dialogs. Download failures log the underlying exception, restore the form, and show the shared error notification so the user can correct the inputs or retry.

## 8. Python-to-browser freshness contract

`DashboardWarnings` creates hidden metadata with `data-last-full-update`, `data-last-live-update`, full/live thresholds, and heartbeat timeout. It sends `window.dashboardHeartbeat?.()` every five seconds. Full freshness uses last persisted collection time; live freshness uses last successful live collection time, not UI render time.

[warnings.js](../../solar_model/dashboard/assets/warnings.js) reads DOM dataset strings, parses timestamp ages, checks navigator/socket/heartbeat, and owns the fixed warning banner. Offline/online and socket disconnect/connect events invalidate the heartbeat; a fresh heartbeat is required to clear that condition. Visibility/pageshow and native resume events instead evaluate wall-clock age, preserving an unexpired heartbeat across brief tab switches. A gap exceeding 20 seconds still invalidates it. Stale timestamps keep the banner visible even when connection is healthy.

[live_updates.js](../../solar_model/dashboard/assets/live_updates.js) tracks a three-second wall-clock expiry for the three-dot update indicator. A new reading restarts the finite animation in [live_updates.css](../../solar_model/dashboard/assets/live_updates.css), which finishes hidden without a JavaScript timeout or animation-end callback. Warnings publish live health through `setLiveUpdateHealth`; an unhealthy connection, heartbeat or live reading suppresses and clears the dots without restarting them on recovery. Persisted database staleness still shows its banner but does not suppress receipt of healthy live readings. Visibility loss, pagehide and freeze clear it; visibility/pageshow/native resume also check expiry. Each of the three dots repeats a smooth pulse whose end matches its start; the enclosing indicator expires after three seconds. Reduced-motion mode shows static dots until expiry.

`RecentTab` renders the persistent small grey label below Live / Today. The browser formats the UTC reading timestamp from warning metadata as `YYYY-MM-DD HH:mm:ss` in its own local timezone, without a timezone suffix. The freshness check and lifecycle events refresh this label after initial rendering, full snapshot application, new readings and resume; browser DST rules apply. It represents the sample collection time, never browser render time. A missing timestamp displays Last reading: awaiting live data. Saved offline copies can preserve the timestamp while in-page scripts are inactive; no in-page connection-status guarantee is made for Chrome offline archives. Action notifications use shared Python helpers; persistent warning policy is not duplicated in tab callbacks.

Data-only records (`TelemetryData`, `AnalysisRangeData`, `LivePowerData`) live in `storage/queries/types.py`. UI references and browser state live in `dashboard/state.py`; row/number formatting lives in `dashboard/formatting.py`. SQL and dataframe adapters are in `storage/queries/`.

## Plan tab ownership and refresh

`DashboardLayout` creates a per-browser `PlanTab` and mounts it between Analysis and System Info. Snapshot updates remain limited to Recent and System Info; they do not trigger a solve or overwrite the plan. The Plan tab initially renders controls and an empty-state hint only.

`PlanTab.refresh()` validates adjustment, guards against duplicate requests and sets loading visibility before awaiting `run.cpu_bound(plan_with_preference, config, as_of, preference)`. `finally` restores the controls. The service uses a replaced configuration and opens the existing schema directly, closing its database in `finally`. Its picklable `PlanReport` contains `simulation: SimulationResult`, `actuals: DataFrame` and `battery_capacity_kwh: float | None`; the worker configures logging so model timing/complexity logs remain visible. No browser elements are sent to the worker.

`scenario_summary()` sums costs, solar/house-load and import/export energy, includes both opening and end levels when finding minimum battery, and uses actual durations for below-reserve hours and the exact diminishing-return protection score. Scenario-average cost is the arithmetic mean because `create_scenarios` assigns each of the nine scenarios probability 1/9. It is not a statistical confidence interval.

`plan_chart_options()` supplies ten ECharts line segments with five shared names/legend entries: solid actual and dashed forecast for each metric, including Grid Import and Grid Export. Forecast power is interval energy divided by interval hours, rendered as steps; forecast battery joins the initial state to each end state. Actuals retain null minute rows (`connectNulls=False`). Tooltip labels distinguish actual/forecast, and timezone-aware JavaScript formatting uses the configured timezone. The scenario selector replaces series on the existing chart and rebuilds summary/detail rows without another solve. Stable metric/actual-or-forecast series IDs and `setOption` with `replaceMerge=["series"]` preserve legend selections and data zoom. Animation is disabled so the complete forecast appears immediately. `detail_rows()` copies the frame, formats local start/end times as HH:MM and rounds numeric cells to four decimals while retaining numeric types and missing-value nulls. The original simulation result stays in physical units and UTC.

Plan detail uses `pagination=None` to display every row and hide pagination controls. Plan header sorting is disabled and row order remains chronological despite the HH:MM-only display; no hidden sort timestamps are needed. Repeated successful refreshes rebuild the summary from the returned result; browser regression coverage changes the adjustment and verifies changed returned costs replace the earlier summary. Equal optimal outcomes across preferences remain valid model results.

Scenario summary label/value pairs render in a single bordered grid above the chart; the detail table remains below it. Solar/House Load totals are summed forecast kWh over the selected scenario's complete projection, updated alongside the existing summary on scenario changes.

The Plan chart sets `alignTicks=True` on the battery y-axis so its tick/gridline positions align with the power axis while retaining independent kWh and kW scales.

The Plan detail table has a grey bold Total row, fixed below the period rows. It sums interval kWh flows/losses and import cost, export revenue and net cost from unrounded model values. Timestamps (apart from the Total label), tariffs, rates, power limits and opening/closing/minimum/maximum battery levels are blank; entirely missing energy columns remain blank.

Plan detail includes a **Plan action** column immediately after Start and End: Self-consumption, Force charge or Force discharge. The first row uses the shared next-action mode; later rows infer the action from grid-to-battery charging or battery-to-grid export above 0.000001 kWh, ignoring numerical noise. Later actions describe scenario flows, not additional solved device commands. The Total row leaves this column blank.

**Download Excel** beside Plan detail exports the selected scenario to an `.xlsx` workbook, with the selected Compact/Advanced columns, local HH:MM times, plan actions, SOC, rounded numeric cells and Total row as the table (in original chronological order). Hidden sorting fields are excluded. The filename includes the scenario and local plan start; the worksheet is named Plan detail. Export reuses the shared XlsxTurbo writer and does not recalculate the plan.

All dashboard table headers stay below the dashboard topbar during page scrolling and leave the viewport at the end of their own table. Shared `assets/table_headers.js` translates the real header within the table bounds on scroll/resize, preserving column widths, sorting and horizontal scrolling without a second header or an inner vertical scrollbar. Updates use animation frames; child-list observation also handles newly displayed tables and refreshed rows. Styling applies to all Quasar table headers.

The Plan top section puts adjustment/help tooltip and Refresh/loading in one control row, and the next action on its own line, labelled `Action for next X mins - ...`. Solar and Load button groups share the following wrapping row with scenario-average cost/range. Successful refresh hides the status label; only initial guidance and errors show status. Changing preference updates the Refresh enabled state without a message. The time-range banner, equal-scenario explanation and actual/forecast hint are omitted. Summary cards are replaced by inline label/value cells in one bordered grid. `detail_frame()` copies a scenario frame and inserts start/end SOC beside their stored-kWh columns using the report nameplate capacity (energy/capacity * 100); unknown capacity yields blank SOC. It also sums the three columns ending `_loss_kwh` into `losses_kwh`; original loss routes remain available in Advanced. State/SOC fields are never summed in totals.

Compact detail columns, in order: `starts_at`, `ends_at`, `plan_action`, `tariff_name`, `import_rate`, `export_rate`, `load_kwh`, `solar_kwh`, `losses_kwh`, `solar_clipped_kwh`, `grid_import_kwh`, `grid_export_kwh`, `battery_end_kwh`, `battery_end_soc_percent`, `import_cost`, `export_revenue`, `net_cost`. Advanced includes every source column plus derived SOC/losses. The selected view is kept when switching scenarios or refreshing; view changes rebuild only detail. Excel uses the visible column selection and order.

`hourly_detail_frame()` groups Compact view into configured-local clock hours keyed by their UTC instants, preserving repeated DST hours. It splits source intervals at clock-hour boundaries, prorates all flow/loss/clipping energies and costs, and interpolates start/end battery kWh/SOC within the original interval. Hourly energies and costs sum with missing values retained; ending state uses the final segment, opening state the first. Import/export rates use valid-duration-weighted means; action and tariff names join distinct values in occurrence order. Mixed first-hour actions are preserved instead of being overwritten by the shared next action. Advanced bypasses hourly aggregation. Excel exports the corresponding grouped or original-period frame.

`detail_decimal_places()` controls browser numeric formatters and Total cells: rates four decimals, SOC one, other numeric values two. Underlying row values stay numeric; header sorting is disabled. Values retain their existing four-decimal preparation for table/export, while aggregation happens before rounding.

Plan detail uses short presentation headers (Action, Tariff, House, Loss, Clipped, Import, Export, Battery, SOC, Cost, Revenue, Net), retaining units and original field keys. Advanced restores full descriptive route/field names, including Solar To Battery Loss, Battery end SOC and Net cost; Compact keeps the short labels. Excel uses the selected view's headers. All Plan columns use `sortable=False`. Two standard NiceGUI `ui.toggle` controls replace the dropdown: one each for Solar and Load, with Low (0.5x), Base and High (1.5x). Each has a single non-clearable value, initially base. The `scenario_name` property combines load/solar selections (`base` for both base, otherwise `load_solar`) for chart, summary, detail and Excel. Factors come from the same scenario definitions used by planning. Changing either control invokes the existing scenario render without another solve; the former dropdown-specific key handlers are removed.

`applied_adjustment` records the preference only after a successful render. Refresh starts enabled, becomes disabled/grey after success, and `_adjustment_changed()` enables it only when the current value differs (reverting disables it again). Failed attempts restore an enabled button for retry. Loading still hides Refresh and disables the adjustment.

Solar/Load scenario controls use individual bordered fieldsets with centred legends above the buttons. Unselected buttons use Quasar grey-2 with dark text; the selected button retains the primary colour and white text. The groups have a 24px gap (twice the former 12px) and wrap on narrow screens. Border/legend rules are scoped to `.plan-scenario-group`.

Advanced Plan detail adds `.plan-detail-advanced`: 13px text, 6px/12px cell padding, headers at least 110px wide (70px for Start/End) and up to 220px before wrapping. Compact retains its tighter existing styling; both remain horizontally scrollable.
