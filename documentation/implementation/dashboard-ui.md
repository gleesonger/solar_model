# Dashboard live state, caching, and presentation

`dashboard.tabs.plan.expected_plan_summary()` formats Markov expected visible cost and full-horizon score from dedicated result fields. Selected-scenario details remain stress paths, labelled in the summary and Excel export. Other models retain the existing presentation; see the [Markov consumer contract](markov-dynamic-programming.md#consumers).

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

The store's lock protects snapshot/listener references. It does not lock database readers or freeze pandas contents. Failed refresh leaves the previous snapshot installed. While browsers are subscribed, `run_forever()` waits the configured interval after each attempt, so refresh cost adds to cadence.

Only a connected browser subscribes to snapshot and plan publications; an HTTP page request alone does not subscribe. Without subscribers, `DashboardSnapshotStore.run_forever()` waits on a wake event without querying. The first connection triggers the initial load. Further connections reuse a fresh snapshot; reconnecting after the refresh interval wakes a new refresh. Disconnect wakes the worker so it can suspend again, and failures retry at the normal interval rather than spinning. Cached snapshots remain available while idle.

The dashboard uses an explicit per-browser root page, so startup stops the unused NiceGUI auto-index client's outbox. Real browser outboxes remain active. `ui.run(binding_refresh_interval=0.25)` polls bindings four times per second; direct table/chart updates and the live-reading interval remain independent. The ASGI server still performs its own maintenance ticks while idle.

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

`latest_key` and orientation flag are metadata for updates/style; visible fields are formatted strings. Missing numeric values become empty strings. The Recent Live/Today Latest (kW) column uses two decimal places on initial load and live updates, including orientation rows; rounded negative zero displays as `0.00`. Other energy/power cells use one decimal, and currency summaries use two decimals. Per-orientation forecast cells are not filled just because panel forecast columns exist; the solar forecast maps contain site solar totals only. House Load has its own BoD total from the saved daily house profile, with Latest/Raw Forecast left blank.

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

The **Holiday periods** section loads `holiday_period` into an editable AG Grid (inclusive local start/end dates). Both date columns use AG Grid's `dateString` type and `agDateStringCellEditor`, whose native browser date input returns `YYYY-MM-DD`; stored/displayed cells remain ISO strings, although the editor's visible date format follows browser locale. A read-only **Days** column calculates `end - start + 1` from the date strings in UTC, so daylight-saving changes do not affect the count; incomplete or reversed dates show blank. It updates as date cells change and is not stored in the database. Adding a blank row does not write to storage. The first committed date edit fires `cellValueChanged`; Python parses the ISO value as a date, and a missing opposite date is copied from it to create a one-day period. Each valid edit and selected-row removal atomically replaces the stored ranges, with no Save button. Invalid dates, end-before-start ranges and overlaps are rejected without changing storage; the row remains editable for correction. The edited row is brought back into view after a grid refresh. Changes affect new load forecasts and subsequent plan calculations without a restart. On first Control activation, AG Grid's built-in `ensureIndexVisible` scrolls to the final row. Immediately below the grid, month inputs and a confirmation dialog run `backfill_daily_house_load_forecasts()` in `run.io_bound`. Backfill covers both selected months inclusively through today, atomically replaces each day's profile, and reports recalculated days/hourly rows.

## 8. Python-to-browser freshness contract

`DashboardWarnings` creates hidden metadata with `data-last-full-update`, `data-last-live-update`, full/live thresholds, and heartbeat timeout. It sends `window.dashboardHeartbeat?.()` every five seconds. Full freshness uses last persisted collection time; live freshness uses last successful live collection time, not UI render time.

[warnings.js](../../solar_model/dashboard/assets/warnings.js) reads DOM dataset strings, parses timestamp ages, checks navigator/socket/heartbeat, and owns the fixed warning banner. Offline/online and socket disconnect/connect events invalidate the heartbeat; a fresh heartbeat is required to clear that condition. Visibility/pageshow and native resume events instead evaluate wall-clock age, preserving an unexpired heartbeat across brief tab switches. A gap exceeding 20 seconds still invalidates it. Stale timestamps keep the banner visible even when connection is healthy.

[live_updates.js](../../solar_model/dashboard/assets/live_updates.js) tracks a three-second wall-clock expiry for the three-dot update indicator. A new reading restarts the finite animation in [live_updates.css](../../solar_model/dashboard/assets/live_updates.css), which finishes hidden without a JavaScript timeout or animation-end callback. Warnings publish live health through `setLiveUpdateHealth`; an unhealthy connection, heartbeat or live reading suppresses and clears the dots without restarting them on recovery. Persisted database staleness still shows its banner but does not suppress receipt of healthy live readings. Visibility loss, pagehide and freeze clear it; visibility/pageshow/native resume also check expiry. Each of the three dots repeats a smooth pulse whose end matches its start; the enclosing indicator expires after three seconds. Reduced-motion mode shows static dots until expiry.

`RecentTab` renders the persistent small grey label below Live / Today. The browser formats the UTC reading timestamp from warning metadata as `YYYY-MM-DD HH:mm:ss` in its own local timezone, without a timezone suffix. The freshness check and lifecycle events refresh this label after initial rendering, full snapshot application, new readings and resume; browser DST rules apply. It represents the sample collection time, never browser render time. A missing timestamp displays Last reading: awaiting live data. Saved offline copies can preserve the timestamp while in-page scripts are inactive; no in-page connection-status guarantee is made for Chrome offline archives. Action notifications use shared Python helpers; persistent warning policy is not duplicated in tab callbacks.

Data-only records (`TelemetryData`, `AnalysisRangeData`, `LivePowerData`) live in `storage/queries/types.py`. UI references and browser state live in `dashboard/state.py`; row/number formatting lives in `dashboard/formatting.py`. SQL and dataframe adapters are in `storage/queries/`.

## Plan tab ownership and refresh

`PlanColumn` has no constructor defaults: every row explicitly supplies all nine fields, including `per_hour` and `export`, with columns aligned in source. Definitions are constructed with `table_to_object_list` and indexed by DataFrame column name in `PLAN_COLUMNS`. Dictionary insertion order defines Advanced order after `ADVANCED_HIDDEN_COLUMNS` filters presentation-only omissions; `compact_order` selects and orders Compact columns and `compact_label` optionally overrides the Advanced header. `ADVANCED_LABELS` supplies short wrapped headers without changing field keys. `format` selects local HH:MM, text or numeric preparation/alignment; `decimals` controls browser/footer precision independently of four-decimal numeric preparation. `detail_columns()` generates shared browser/Excel headers. `per_hour` marks cost, protection and limit contributions divided by elapsed hours for browser display only; terminal and total contributions retain their period values. `export=False` excludes internal `t` from Excel.

Each column has one `Aggregation` flag governing splitting/grouping/footer behavior:

| Flag | Split at clock hours | Hourly grouping | Footer |
| --- | --- | --- | --- |
| `QUANTITY` | Prorate by elapsed duration | Sum, preserving entirely missing values | Sum |
| `AVERAGE` | Repeat | Mean weighted by valid elapsed duration | Blank |
| `OPENING_STATE` | Interpolate paired start/end state | First | Blank |
| `CLOSING_STATE` | Interpolate paired start/end state | Last | Blank |
| `SETTING` | Repeat | Last | Blank |
| `TEXT` | Repeat | Join distinct non-null values in occurrence order | Blank |
| `START_TIME` | Segment start | First | Total label |
| `END_TIME` | Segment end | Last | Blank |
| `DURATION` | Segment elapsed hours | Sum | Sum |

State interpolation pairs `_start_` and `_end_` field names. Scheduled bounds/export limits retain last-value behavior. `detail_column()` supplies compatibility defaults for unlisted Advanced fields: generated label, numeric formatting (one decimal for SOC suffixes, otherwise two), quantity aggregation for `_kwh`, otherwise setting aggregation. Such fields appear after listed Advanced fields and stay hidden in Compact. Known column semantics come exclusively from metadata, rather than suffix rules. SOC derivation, conversion-loss calculation and Compact clipping inclusion remain explicit calculations on copied frames.

`DashboardLayout` creates a per-browser `PlanTab` and mounts it between Analysis and System Info. Recent snapshot updates remain limited to Recent and System Info. A separate server-owned `PlanStore` supplies the current shared plan on initial rendering and reconnect and publishes new results through `client.safe_invoke`; disconnect removes the subscription. Before the first result the tab shows a waiting/loading state.

`PlanTab` holds the startup `config`, a reference to `PlanStore`, an optional per-browser `energy_plan_override`, the displayed report and run metadata. `live_config` reads the store's current saved planning configuration. **Plan settings** uses a 938px dialog card, capped at the viewport width minus 48px, with an 882px desktop textarea and mobile viewport cap. Run Demonstration validates an override, closes the editor and calculates a private demonstration. Load Global replaces only the draft with the live saved settings; Cancel discards the draft. Confirmed Save To Disk atomically replaces only `energy_plan` in the startup file, updates the store's running planning settings and clears this browser's override. Other source text is preserved. The editor retains compact scenario/refinement mappings, grouped identical weekday rules and collapsed timestep runs. Invalid drafts or failed saves preserve the previous settings.

`PlanStore.run_forever()` observes the latest committed plan ID and planning-service health once per second. It loads only changed IDs, caches one report for all browsers and republishes UI updates. It reloads energy-plan settings for the editor/private demonstrations, but cannot schedule shared plans. The independent `runtime.planner.PlannerService` owns input-marker polling, the cadence tolerance, settings changes and one scheduled calculation worker. Status includes the last successful publication and a five-second heartbeat; stopped or stale status is visible while the last plan remains available.

`dashboard.planning_worker.demonstrate()` owns a separate on-demand spawn worker with one admitted demonstration server-wide and no request queue. Both kinds of worker use the configured calculation time limit, capture input cutoffs inside execution and are reaped after timeout/crash. The dashboard has no solver warm-up at startup. See [runtime ownership](runtime.md) and [saved-plan contract](plan-history.md).

`PlanTab.run_demonstration()` guards duplicate browser requests and disables its settings control while awaiting a private demonstration. The settings dialog Run Demonstration button uses this path and cannot force a shared run. There is no standalone demonstration button on the Plan tab. While following latest, every shared publication replaces the displayed result, including demonstrations, preserving scenario choices, view, legend selections and zoom. A demonstration records the shared version at request start and is discarded if that version advances before it finishes. The red bracketed demonstration caption is visible only for private results. The service validates a request-local config, opens the existing schema and closes its database in `finally`. Its picklable `PlanReport` retains simulation, actuals, capacities, scenario settings, reserve, preference and source-reading timestamp. These fields keep labels and summaries tied to the displayed result; no browser elements go to the process worker.

The action caption converts the timezone-aware first-interval timestamp using the standard datetime `astimezone()` interface, without wrapping it in a new pandas timestamp.

`scenario_summary()` sums costs, solar/house-load and import/export energy, includes both opening and end levels when finding minimum battery, and uses actual durations for below-reserve hours and the exact diminishing-return protection score. The displayed min/max cost range covers the retained fixed-condition stress cases; it is not a statistical confidence interval or Markov expected cost.

`plan_chart_options()` supplies ten ECharts line segments with five shared names/legend entries: solid actual and dashed forecast for each metric, including Grid Import and Grid Export. Forecast power is interval energy divided by interval hours, rendered as unsmoothed end steps for Grid Import/Export so zero-flow periods stay zero. Solar and House Load use gently smoothed lines (`smooth=0.2`, `smoothMonotone="x"`); forecast battery joins the initial state to each end state with the same smoothing. Smoothing affects presentation only; interval energies, model controls and table/export values are unchanged. Actuals retain null minute rows (`connectNulls=False`). Tooltip labels distinguish actual/forecast, and timezone-aware JavaScript formatting uses the configured timezone. Scenario changes and successful plan refreshes replace series on the existing chart and rebuild summary/detail rows. Refresh updates the action panel and summary values in place, retaining the scenario controls, selected Solar/Load cases and ECharts instance. Scenario changes do not run another solve. Stable metric/actual-or-forecast series IDs and `setOption` with `replaceMerge=["series"]` preserve legend selections and data zoom. Animation is disabled so the complete forecast appears immediately. `detail_rows()` copies the frame, formats local start/end times as HH:MM and rounds numeric cells to four decimals while retaining numeric types and missing-value nulls. The original simulation result stays in physical units and UTC.

Plan detail uses `pagination=None` to display every row and hide pagination controls. Plan header sorting is disabled and row order remains chronological despite the HH:MM-only display; no hidden sort timestamps are needed. Repeated successful refreshes rebuild the summary from the returned result; browser regression coverage changes the YAML protection preference and verifies changed returned costs replace the earlier summary. Equal optimal outcomes across preferences remain valid model results.

Scenario summary label/value pairs render in a single bordered grid above the chart; the detail table remains below it. Solar/House Load totals are summed forecast kWh over the selected scenario's complete projection, updated alongside the existing summary on scenario changes.

The Plan chart sets `alignTicks=True` on the battery y-axis so its tick/gridline positions align with the power axis while retaining independent kWh and kW scales.

Compact Plan detail has one grey bold Total row below the periods. Advanced has two subtotal rows (0–24h and 24–48h, or the remaining horizon) followed by the full Total. `advanced_detail_totals()` divides any crossing interval in proportion to elapsed seconds and assigns terminal score to the subtotal containing the interval end; `total_score` is split consistently with that terminal contribution. All footer rows sum kWh flows/losses and import cost, export revenue and net cost from unrounded model values. Timestamps (apart from the row label), tariffs, rates, power limits and opening/closing/minimum/maximum battery levels are blank; entirely missing energy columns remain blank.

Plan detail includes a **Plan action** column. DP records Self-consumption, Force charge or Force discharge for every period; the first policy is shared across scenarios and later choices are scenario-specific. MILP reports its first selected policy and later flow-based labels. `detail_rows()` substitutes `Export Solar Excess` when PV export is positive and a Force charge/discharge rate is zero. The first row uses `NextAction`'s requested power ceiling; later rows use the corresponding battery flow as a display proxy because the report does not retain each later command rate. `next_action_description()` applies the same first-row label to the top action panel and updates it with the selected scenario. This changes only display and Excel labels. Compact joins distinct actions within each hour; Advanced and Excel preserve selected-view labels.

**Download Excel** beside Plan detail exports the selected scenario to an `.xlsx` workbook, with the selected Compact/Advanced data columns, full configured-local Start/End timestamps (`YYYY-MM-DD HH:MM:SS`), plan actions, SOC, rounded numeric cells and the selected view's footer rows; it omits display-only hours and retains original period score contributions (in original chronological order). Multi-line table headers become single-line workbook headers. Hidden sorting fields are excluded. The filename uses the successful result's model and protection adjustment, the selected view (`com` for Compact, `adv` for Advanced), explicit Solar/Load scenario names and configured-local plan start: `plan-MILP-com-protection-75-solar-high-load-low-20261031-1005.xlsx`. Pending control changes do not relabel the existing result's download; the worksheet is named Plan detail. Export reuses the shared XlsxTurbo writer and does not recalculate the plan.

All dashboard table headers stay below the dashboard topbar during page scrolling and leave the viewport at the end of their own table. Shared `assets/table_headers.js` translates the real header within the table bounds on scroll/resize, preserving column widths, sorting and horizontal scrolling without a second header or an inner vertical scrollbar. Updates use animation frames; child-list observation also handles newly displayed tables and refreshed rows. Styling applies to all Quasar table headers.

The Plan top section puts Plan settings and loading feedback in one wrapping control row. The next action appears in a pale-blue filled, bordered panel with a small `Next X mins - As of ...` caption and a separate medium-weight command description. The caption formats the first interval's start in the configured timezone, including weekday, date, year and time, and is updated with each successful refresh. Model, UI-override and missing-actuals labels are omitted. Solar and Load button groups occupy the next row. The Net cost summary cell shows the selected scenario's visible-horizon cost (three decimals) with the minimum/maximum scenario costs (two decimals), for example `?0.212 (-?2.44 to ?2.40)`. Negative signs precede the currency symbol. The leading cost updates with the selected Solar/Load scenario; the range continues to cover all scenarios. Successful refresh hides the status label; initial guidance and errors show status. Model selection lives in optimiser.model inside the YAML editor. Run Demonstration reruns the plan automatically; Load Global only changes the editor draft; Run Demonstration reruns the plan. The settings control is disabled during calculation and restored on success/failure. Export model/preference metadata remains tied to the successful request. The time-range banner, equal-scenario explanation and actual/forecast hint are omitted. Summary cards are replaced by inline label/value cells in one bordered grid. `detail_frame()` copies a scenario frame and inserts start/end SOC beside their stored-kWh columns using the report nameplate capacity (energy/capacity * 100); unknown capacity yields blank SOC. It also sums the four stage-specific columns ending `_loss_kwh` into `losses_kwh`; original loss routes remain available in Advanced. State/SOC fields are never summed in totals.

Compact detail columns, in order: `starts_at`, `ends_at`, `t` (shown as **hours**), `plan_action`, `tariff_name`, `import_rate`, `export_rate`, `load_kwh`, `solar_kwh`, `losses_kwh`, `grid_import_kwh`, `grid_export_kwh`, `battery_end_kwh`, `battery_end_soc_percent`, `import_cost`, `export_revenue`, `net_cost`. Advanced includes the other source routes and scores, but omits battery start, solar/battery-to-inverter and individual stage-loss columns. It retains `losses_kwh`, ending battery kWh/SOC and unknown future fields. The selected view is kept when switching scenarios or refreshing; view changes rebuild only detail. Excel uses the visible column selection and order.

`hourly_detail_frame()` groups Compact view into configured-local clock hours keyed by their UTC instants, preserving repeated DST hours. It splits source intervals at clock-hour boundaries, prorates all flow/loss/clipping energies and costs, and interpolates start/end battery kWh/SOC within the original interval. Hourly energies and costs sum with missing values retained; ending state uses the final segment, opening state the first. Import/export rates use valid-duration-weighted means; action and tariff names join distinct values in occurrence order. Mixed first-hour actions are preserved instead of being overwritten by the shared next action. Advanced bypasses hourly aggregation. Excel exports the corresponding grouped or original-period frame.

`detail_decimal_places()` controls browser numeric formatters and Total cells: rates four decimals, SOC one, other numeric values (including t and scores) two. Underlying row values stay numeric; header sorting is disabled. Values retain their existing four-decimal preparation for table/export, while aggregation happens before rounding.

Plan detail uses short presentation headers while retaining original field keys. Advanced route headers are directional (`S→L`, `S→B`, `S→G`, `G→L`, `G→B`, `B→L`, `B→G`); the key above the table defines S/B/L/G and arrow direction. Header text explicitly breaks before units, with `white-space: pre-line` and Advanced width bounds, while Excel replaces those newlines with spaces. Compact keeps its short labels. All Plan columns use `sortable=False`. Two standard NiceGUI `ui.toggle` controls replace the dropdown: one each for Solar and Load, with exactly Low, Med and High, each labelled with its configured multiplier. Each has a single non-clearable value, initially med. The `scenario_name` property combines load/solar selections (`med` for both med, otherwise `load_solar`) for chart, summary, detail and Excel. Factors come from `config.energy_plan.scenario_multiplier.solar` and `config.energy_plan.scenario_multiplier.house_load`, shared with planning. Changing either control invokes the existing scenario render without another solve; the former dropdown-specific key handlers are removed.

`applied_adjustment` records the scalar protection preference from the successful request for Excel filenames; it is not a second settings overlay. Plan settings remains available whenever no demonstration is running, allowing the same settings to be used again from the dialog. Load Global only changes the editor draft; Run Demonstration immediately starts a new run; failures permit retry.

Solar/Load scenario controls use individual bordered fieldsets with centred legends above the buttons. Unselected buttons use Quasar grey-2 with dark text; the selected button retains the primary colour and white text. The groups have a 24px gap (twice the former 12px) and wrap on narrow screens. Border/legend rules are scoped to `.plan-scenario-group`.

Compact and Advanced Plan detail use the same `.plan-detail-table` styling: 0.75rem text, 4px vertical/3px horizontal cell padding, wrapping headers capped at 9rem, and non-wrapping numeric cells with tabular figures. Both share column sizing rules, margins and spacing; there are no Advanced-only size overrides. Actual column widths follow their header/data content, and both views remain horizontally scrollable.

Advanced Plan detail displays Cost Score, Protection Score and Limit Score per hour, while Terminal Score and Total Score retain original period contributions, with two decimal places. Cost Score is negative net cost. Protection Score is the tariff/preference-scaled reward, using the selected optimiser's protection curve (MILP piecewise-linear, DP exact nonlinear) and duration weights; it is not the raw 0-100 protection utility. Total Score includes cost, protection and final-row terminal contributions. The selected scenario scores are unweighted; averaging visible and hidden scenario totals with normalised probabilities gives the overall optimisation objective (within solver tolerance). Summary statistics remain limited to the visible horizon. Advanced appends lookahead rows (configured as four-hour MILP steps or hourly DP steps, with possible partial edge intervals) to its table, chart and Excel download; Compact excludes them. Compact omits score columns; Advanced Excel includes them.

`table_detail_frame()` copies the selected detail frame, derives internal `t` = elapsed seconds/3600 (labelled **hours**) and divides only cost, protection and limit score contributions by that duration. Terminal and total score retain their period contributions; the terminal credit is never divided by a partial final interval. Browser score headers remain short names without a /h suffix. It runs after Compact hourly aggregation, so partial-hour weights remain correct; Advanced uses original periods. Footer scores use original unnormalised contributions; footer hours sum durations within each span. Formatting happens afterwards. `_download_detail()` receives the original frame, omits the display-only hours column, and exports original contributions with the same footer split as the table. Model scores and summaries are not altered.

Protection adjustment has a minimum of zero and no maximum, defaulting to 50 through EnergyPlanConfig/YAML. UI, service and configuration validation all reject negative or non-finite values. X/100 remains the multiplier: values above 100 increase V beyond the reserve replacement value. The raw protection utility remains bounded by 100 points.

Advanced Plan columns follow this reading order: timing/action; tariffs; household demand/solar generation; directional energy routes and inverter conversion totals; aggregate losses and grid totals; battery constraints and ending kWh/SOC; financial outcomes; scoring. `ADVANCED_HIDDEN_COLUMNS` removes battery-start, solar/battery-to-inverter and individual stage-loss columns from the table and Advanced Excel, while the original frame and `detail_frame()` retain them for calculations. `ADVANCED_LABELS` supplies short, intentionally split headers. Route abbreviations are defined above the table. In `layout.py`, the first five Advanced columns (Start through Tariff) use the original 3px padding and 9rem maximum; columns from Import rate onward use 3.2px padding and 4.4–9.6rem header bounds. Advanced adds 4rem of blank space after the table to keep the last row clear of bottom notifications. Compact is unchanged. New unlisted fields remain visible at the end.

Compact combines conversion losses and solar clipping in one Losses (kWh) column before hourly aggregation; the same combined values and totals appear in Compact Excel. Advanced keeps conversion losses and solar clipping separate. The change is applied to a copied display frame, leaving model results unchanged.

`PlanTab._scenario_frame()` concatenates the selected scenario's visible and lookahead frames only in Advanced mode. `_detail_view_changed()` updates the existing chart series and detail table through `_render_scenario()` without another solve. Summary statistics continue to use the visible frame; Advanced totals/Excel cover the full model horizon. Results without lookahead rows remain supported.

The Plan action summary shows the selected mode and power ceiling, with an optional controller target in kWh and SOC percent. The target is monitored by a future controller and is not a device SOC cut-off. The same action can achieve different power across scenarios.

The settings footer places an outlined blue Save To Disk button at the left and Load Global, Cancel and the filled Run Demonstration commit button at the right on the same wrapping row. Saving first opens a confirmation dialog; cancelling writes nothing. Confirmed saving leaves the editor open and shows a positive notification. Saving does not apply the draft to the browser overlay or rerun the plan; Run Demonstration performs both. optimiser.model accepts only mix_integer_linear_prog and dynamic_programming; the serializer always places `# model - mix_integer_linear_prog, dynamic_programming` above the model entry. The standalone Model selector is removed.

In Advanced detail, `advanced_plan_frame()` shows only `Self-consumption` for that mode. Force charge/discharge and `Export Solar Excess` show average **import** or **export** power (grid energy divided by actual elapsed hours) and projected end battery kWh/SOC rounded to whole numbers, for example `Force discharge: export 2.00 kW to 4kWh/21%`. The action mode still uses the shared first command and inferred later flows. The import value includes household demand and conversion losses; the export value can include solar export. These are projected period averages/end states, not requested battery rates, additional device power ceilings or SOC-stop commands. Missing capacity omits the percentage. The same enriched text is exported in Advanced Excel; Compact retains its short action labels. The preparation copies the display frame and leaves model results unchanged; action totals remain blank.

DP grid infeasibility reaches Plan refresh as `NoFeasiblePlanError`, or as a NiceGUI `SubprocessException` with that original type. Refresh extracts only the original message, logs a warning without traceback, and shows the concise reason in the error notification. The previous result remains visible and controls permit retry; unexpected errors still log tracebacks.

DP Advanced detail and Excel include Limit Score for scheduled battery Min/Max breaches. A negative value identifies an interval-end breach; zero means the ending level meets the preferences. Like other scores, the browser shows an hourly rate and the footer sums original duration-weighted contributions; Excel exports those original contributions. DP Total Score includes Limit Score, while Net cost remains actual model import cost minus export revenue. Compact omits score columns. MILP retains its existing score columns and hard Min/Max limits.

Advanced detail shows projected ending kWh/SOC and grid flows for the selected policy. Zero-power variants with PV export show `Export Solar Excess` in the UI.

Mode 5 at zero discharge allows surplus solar to charge in the model; that installed-firmware response remains unverified.

## Database-backed shared plans

The worker now prepares and commits both detail views before returning a plan ID. The server reads the committed report once and notifies clients using the existing subscriber mechanism. Startup restores the latest saved report. The Plan tab includes date/snapshot selection to the right of settings, with today's date and a preloaded snapshot list; Snapshot is blank by default for latest. Historical selection uses the same action, summaries, chart, table and export and remains pinned during new publications. See the [full saved-plan contract](plan-history.md).

Plan Settings initially displays the archived energy-plan YAML of the displayed snapshot unchanged. Loading a report does not validate that YAML against the current model configuration. Run Demonstration and Save To Disk validate the draft against the current schema and report incompatible or missing settings. Load Global explicitly replaces the draft with the current live configuration.

Plan snapshots store projection data and settings without chart actuals. Latest and historical charts query telemetry separately through the earlier of now and the projection horizon. Displayed charts refresh actuals every minute without rerunning the optimiser or replacing a pinned forecast. One legend entry per metric controls both solid actual and dashed forecast lines at overlapping timestamps. Legacy embedded actuals are ignored. See [saved-plan contracts](plan-history.md).

The Plan chart has Line/Bar power buttons on the legend row, aligned to the right. Line is the initial display. Switching affects both actuals and forecasts for Solar, House Load, Grid Import and Grid Export. Battery actuals always remain solid lines and battery forecasts dashed lines on the kWh axis. Bar mode groups power into configured-local clock hours and shows average kW: forecast interval energy is split at hour boundaries and weighted by covered duration, while actuals average available minute readings. Partial hours use available coverage; missing observations stay null, without zero filling. Repeated autumn hours retain separate UTC buckets. All bars use the hour midpoint. Actual bars are solid-coloured and forecast bars have a diagonal pattern and reduced opacity, sharing one legend toggle per metric. A scrollable legend reserves space for the controls on narrow screens. The selection is per browser and survives actuals refreshes, scenario/view changes and snapshot selection; legend toggles and zoom are preserved. This display choice does not change saved reports or settings.
