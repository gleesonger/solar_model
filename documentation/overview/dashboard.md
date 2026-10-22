# Dashboard

For exact loaders, filters, intermediate frames, and batched SQL, see [dashboard data processing](../implementation/dashboard-data.md). For live/cache/client ownership and final table/chart/export structures, see [dashboard state and presentation](../implementation/dashboard-ui.md).

Source: [main_dashboard.py](../../main_dashboard.py), [dashboard/layout.py](../../dashboard/layout.py).

The favicon is the amber sun/panel in `dashboard/assets/solar_power.svg`. Startup passes the source-relative file path to NiceGUI, which serves the SVG at `/favicon.ico` and references that URL in the page's icon link. The asset must be valid SVG; restart and reload after deployment. Browsers may cache the fixed icon URL.

## Current tabs

- **Recent:** Live/Today table; latest kW, daily meters, latest/BoD/raw forecast totals; past 7 days and past 12 months energy/economics.
- **Analysis:** lazily loaded energy, power, battery, per-array and money charts; range/grouping/Sum–Average controls; custom chart builder and downloads.
- **System Info:** newest valid metadata value per variable.
- **Control:** confirmed tariff recalculation and managed restart actions.
- `HourlyTab` still exists in [hourly.py](../../dashboard/hourly.py) but is not mounted by the current layout.

## Updates and state

- [DashboardSnapshotStore](../../dashboard/snapshot.py) refreshes shared Recent data and device information in worker threads, once per full interval after work completes.
- Refresh failure retains the last successful snapshot. Successful refresh increments version and publishes to browser subscribers.
- Per-browser `DashboardState`/`DashboardElements` hold controls and element references. Snapshot application updates existing tables/labels; it does not rebuild tabs.
- Browser reconnect resubscribes and reapplies the newest snapshot.
- Live polling uses [LivePowerCollector](../../dashboard/live.py); a per-browser timer applies readings at the configured live interval.
- Daily grid/PV baselines publish only after successful processing. Temporary failures retry without advancing the day or partially updating PV integration.
- The live table remains blank until successful data. Device daily meters drive solar/load/battery totals; grid totals subtract the first persisted same-day lifetime-meter reading or first live reading.
- Per-string voltage/current gives PV proportions. Live PV kW and daily array energy are allocated from plant solar totals using those proportions/integrals; they are not direct independent meters.
- Available battery energy comes from plant availability telemetry; SOC is shown separately.

## Queries, charts, exports

- [data.py](../../dashboard/data.py) owns summary/calendar/forecast helpers and adapters. [chart_data_sql.py](../../dashboard/chart_data_sql.py) aggregates requested chart fields in SQLite and batches shared sources.
- Recent daily/monthly/lifetime summaries share one native SQLite aggregation of hourly actuals and selected forecasts. Python prepares calendar/timezone boundaries and formats the final small tables; it does not aggregate source measurements.
- Energy/cost charts normally use hourly rollups. Single-day telemetry retains minute points; longer ranges use hourly telemetry. Custom battery-level charts can query minute data.
- Import/export power is averaged from separately accumulated grid sign magnitudes, so alternating import/export cannot cancel both series. Meter-energy totals remain separate.
- Energy is summed; telemetry and rates use averages where appropriate. Custom `battery` means available energy level with a no-minute-record fallback; Analysis energy `battery_flow` means charge minus discharge.
- [ChartDataDisplay](../../dashboard/chart_display.py) shares chart/table toggle, clipboard, CSV, labels, and totals. It preserves user legend state during normal updates.
- Analysis downloads support minute/hour/day/week/month/season/year, CSV or Excel, optional forecasts. Interval energy/costs sum; fields ending `_kwh` or `_watt_hours_day` use the last non-null reading, including battery availability levels; other numeric fields average (including cumulative forecast Wh). Import tariff bands retain distinct non-null labels in reading order, joined with ` / ` when an interval spans multiple bands. These are stored-field aggregates; see [metric semantics](../reference/metric-semantics.md) for chart/download differences. Excel strips timezone metadata while retaining displayed local time.
- Changing range/custom selection runs database work via `run.io_bound` and shows progress. Analysis is not automatically refreshed by shared Recent snapshots.
- Downloads aggregate raw samples directly in SQL and load only the selected forecast vintages; disabling forecasts omits their table entirely. A single native SQLite query returns the joined result to pandas; Excel output passes the whole pandas DataFrame to the Rust-backed XlsxTurbo writer, with no application row/cell writing loops. Large minute exports still contain every requested row/field, so their file generation remains more expensive than hourly/daily output.

## Freshness and notifications

- [warnings.py](../../dashboard/warnings.py) publishes hidden timestamp metadata and sends a heartbeat every 5 seconds; [warnings.js](../../dashboard/assets/warnings.js) owns one persistent bottom banner.
- Priority: offline → socket disconnected → heartbeat absent/older than 20 seconds → persisted data stale → live data stale.
- Persisted threshold = 3 × full interval. Live threshold = max(15 seconds, 3 × live interval). Missing timestamps count as stale.
- Brief tab switches retain a heartbeat within its 20-second timeout. Long suspension is detected from wall-clock age; disconnect/reconnect and offline/online events invalidate the heartbeat until a fresh one arrives. No repeated page reload loop is used.
- The hidden-by-default three-dot indicator pulses in repeating cycles for three seconds after a new live reading, using finite CSS animations. It clears on unhealthy connection/heartbeat/live readings, backgrounding, or suspension and checks its absolute expiry on resume. It indicates receipt of a live update, not continued connectivity; a stale persisted-data warning does not suppress healthy live activity.
- Below Live / Today, a small light-grey Last reading label always shows the reading date and time in the browser's local timezone, without a timezone suffix (or awaiting live data). Chrome saved offline pages may not execute dashboard scripts; the recorded timestamp remains useful, but the in-page banner/animation cannot guarantee current status in such a copy.
- Action errors use dismissible top notifications (10 seconds); info/success use 5 seconds. The banner remains until healthy, with a manual Reload action.

See [styling.md](styling.md) for visual conventions and [review findings](../reviews/findings.md) for limitations.
