# Dashboard

For exact loaders, filters, intermediate frames, and batched SQL, see [dashboard data processing](../implementation/dashboard-data.md). For live/cache/client ownership and final table/chart/export structures, see [dashboard state and presentation](../implementation/dashboard-ui.md).

Source: [solar_model/dashboard/app.py](../../solar_model/dashboard/app.py), [solar_model/dashboard/layout.py](../../solar_model/dashboard/layout.py).

The favicon is the amber sun/panel in `solar_model/dashboard/assets/solar_power.svg`. Startup passes the source-relative file path to NiceGUI, which serves the SVG at `/favicon.ico` and references that URL in the page's icon link. The asset must be valid SVG; restart and reload after deployment. Browsers may cache the fixed icon URL.

## Current tabs

- **Recent:** Live/Today table; latest kW, daily meters, latest/BoD/raw forecast totals; past 7 days and past 12 months energy/economics.
- **Analysis:** lazily loaded energy, power, battery, per-array and money charts; range/grouping/Sum–Average controls; custom chart builder and downloads.
- **Plan:** on-demand nine-scenario optimisation, protection adjustment, next-action/day summaries, actual/forecast chart and scenario detail table.
- **System Info:** newest valid metadata value per variable.
- **Control:** confirmed tariff recalculation and managed restart actions.

## Updates and state

- [DashboardSnapshotStore](../../solar_model/dashboard/snapshot.py) refreshes shared Recent data and device information in worker threads, once per full interval after work completes.
- Refresh failure retains the last successful snapshot. Successful refresh increments version and publishes to browser subscribers.
- Per-browser `DashboardState`/`DashboardElements` hold controls and element references. Snapshot application updates existing tables/labels; it does not rebuild tabs.
- Browser reconnect resubscribes and reapplies the newest snapshot.
- Live polling uses [LivePowerCollector](../../solar_model/dashboard/live.py); a per-browser timer applies readings at the configured live interval.
- Daily grid/PV baselines publish only after successful processing. Temporary failures retry without advancing the day or partially updating PV integration.
- The live table remains blank until successful data. Device daily meters drive solar/load/battery totals; grid totals subtract the first persisted same-day lifetime-meter reading or first live reading.
- Per-string voltage/current gives PV proportions. Live PV kW and daily array energy are allocated from plant solar totals using those proportions/integrals; they are not direct independent meters.
- Available battery energy comes from plant availability telemetry; SOC is shown separately.
- All three Recent tables include actual House Load. Live/Today uses measured load power for Latest, stored integrated house energy for Today (updated on full snapshot refresh), and the full saved daily house profile for BoD Forecast. Its Latest Forecast and Raw Forecast cells remain blank; the house forecaster has no separate versions for those columns. House Load Today resets on local date change. Past tables include house actual totals only, without a house-forecast column.
- Live/Today hides the separate Load row, whose Latest power duplicates House Load while its Today energy comes from the device load counter. The Past tables retain both Load and House Load energy.

## Queries, charts, exports

- [query adapters](../../solar_model/storage/queries/dashboard.py) owns summary/calendar/forecast helpers and adapters. [analysis SQL](../../solar_model/storage/queries/analysis.py) aggregates requested chart fields in SQLite and batches shared sources.
- Recent daily/monthly/lifetime summaries share one native SQLite aggregation of hourly actuals and selected forecasts. Python prepares calendar/timezone boundaries and formats the final small tables; it does not aggregate source measurements.
- Analysis Energy by hour includes House Load (Forecast), initially deselected, in House Load blue with the same hatching as Solar Forecast. Recent Live/Today shows the saved full-day house forecast only in its House Load row's BoD Forecast cell; the two Past tables omit house forecasts.
- Energy/cost charts normally use hourly rollups. Single-day telemetry retains minute points; longer ranges use hourly telemetry. Custom battery-level charts can query minute data.
- Analysis Energy includes **House Load** (estimated from load power), selected by default, and the original **Load** device counter, initially deselected. Both remain available in the legend/table/CSV; house load is also selectable in custom energy charts. Missing/long power intervals remain unavailable at storage level, though chart aggregation can display missing energy as zero. Recent actual summaries and economic calculations retain their existing load-counter inputs; forecasting now uses House Load.
- Analysis Power labels the measured household-demand series **House Load** in its legend and table; its internal DataFrame/CSV column remains `load`. Energy **Load** remains the device energy counter; **House Load (Forecast)** predicts household demand in Analysis and supplies the Recent House Load BoD cell.
- Import/export power is averaged from separately accumulated grid sign magnitudes, so alternating import/export cannot cancel both series. Meter-energy totals remain separate.
- Energy is summed; telemetry and rates use averages where appropriate. Custom `battery` means available energy level with a no-minute-record fallback; Analysis energy `battery_flow` means charge minus discharge.
- [ChartDataDisplay](../../solar_model/dashboard/components/chart_display.py) shares chart/table toggle, clipboard, CSV, labels, and totals. It preserves user legend state during normal updates.
- Analysis Energy, Power and Costs include a single series-selection button in their header toolbar. It starts at **Default** and cycles **None → All → Default**; Default restores the chart's original visibility choices. It preserves table/export data and zoom; individual legend entries can still be toggled. Other callers can opt in through `show_series_selection=True`.
- Analysis downloads support minute/hour/day/week/month/season/year, CSV or Excel, optional forecasts. Interval energy/costs sum; fields ending `_kwh` or `_watt_hours_day` use the last non-null reading, including battery availability levels; other numeric fields average (including cumulative forecast Wh). Import tariff bands retain distinct non-null labels in reading order, joined with ` / ` when an interval spans multiple bands. These are stored-field aggregates; see [metric semantics](../reference/metric-semantics.md) for chart/download differences. Excel strips timezone metadata while retaining displayed local time.
- Changing range/custom selection runs database work via `run.io_bound` and shows progress. Analysis is not automatically refreshed by shared Recent snapshots.
- Downloads aggregate raw samples directly in SQL and load only the selected forecast vintages; disabling forecasts omits their table entirely. A single native SQLite query returns the joined result to pandas; Excel output passes the whole pandas DataFrame to the Rust-backed XlsxTurbo writer, with no application row/cell writing loops. Large minute exports still contain every requested row/field, so their file generation remains more expensive than hourly/daily output.

## Freshness and notifications

- [warnings.py](../../solar_model/dashboard/components/warnings.py) publishes hidden timestamp metadata and sends a heartbeat every 5 seconds; [warnings.js](../../solar_model/dashboard/assets/warnings.js) owns one persistent bottom banner.
- Priority: offline → socket disconnected → heartbeat absent/older than 20 seconds → persisted data stale → live data stale.
- Persisted threshold = 3 × full interval. Live threshold = max(15 seconds, 3 × live interval). Missing timestamps count as stale.
- Brief tab switches retain a heartbeat within its 20-second timeout. Long suspension is detected from wall-clock age; disconnect/reconnect and offline/online events invalidate the heartbeat until a fresh one arrives. No repeated page reload loop is used.
- The hidden-by-default three-dot indicator pulses in repeating cycles for three seconds after a new live reading, using finite CSS animations. It clears on unhealthy connection/heartbeat/live readings, backgrounding, or suspension and checks its absolute expiry on resume. It indicates receipt of a live update, not continued connectivity; a stale persisted-data warning does not suppress healthy live activity.
- Below Live / Today, a small light-grey Last reading label always shows the reading date and time in the browser's local timezone, without a timezone suffix (or awaiting live data). Chrome saved offline pages may not execute dashboard scripts; the recorded timestamp remains useful, but the in-page banner/animation cannot guarantee current status in such a copy.
- Action errors use dismissible top notifications (10 seconds); info/success use 5 seconds. The banner remains until healthy, with a manual Reload action.

See [styling.md](styling.md) for visual conventions and [review findings](../reviews/findings.md) for limitations.

Presentation formatting lives in `solar_model/dashboard/formatting.py`; query records are independent of NiceGUI in `solar_model/storage/queries/types.py`. The unmounted Hourly tab, its chart renderers and unused navigation/state fields were removed during the package restructure.

## Plan tab

[PlanTab](../../solar_model/dashboard/tabs/plan.py) owns each browser's adjustment and last result. The protection adjustment (0-100) starts from `energy_plan.protection_preference`; it changes only this request, not YAML or other clients. Zero considers cost only; larger values value retained battery protection more highly. The explanation and configured target reserve are available in the adjustment tooltip.

Refresh captures the current UTC instant and calls `services.planning.plan_with_preference` through `run.cpu_bound`, keeping database assembly and solving off the UI event loop. The Refresh button hides and a spinner appears while running; the adjustment is disabled and duplicate requests are ignored. Failure restores the controls, explains the error and retains the previous successful plan. After a successful run, Refresh is greyed out until the adjustment differs from the last successful value. Reverting to that value disables it again; no adjustment-changed message is shown. Refresh is initially enabled and remains available after a failed attempt for retry. Plans are not generated automatically by tab selection or shared snapshot updates.

The shared next action appears on its own line (for example Action for next 15 mins - Self-consumption), including rate/SOC stop for forced actions. Scenario-average cost and lowest/highest costs remain visible; the time-range banner, success message and explanatory paragraphs are omitted. The nine generated scenarios have equal weight, and scenario labels show their load/solar multipliers (0.5x, 1x or 1.5x). Two compact single-selection button groups, Solar and Load, share a wrapping row with scenario-average cost/range. Each offers Low (0.5x), Base and High (1.5x), initially Base. Their combination selects one of the nine scenarios and updates the day summary, chart and table without another solve. The dropdown and its custom arrow-key handlers are removed. Day summaries sit above the chart and show net cost, total Solar/House Load energy (kWh), grid import/export, minimum/ending stored battery energy, time below reserve and duration-weighted protection. These describe the complete rolling projection, including tomorrow.

The chart has five logical series: Solar, House Load, Battery, Grid Import and Grid Export. Actuals are solid and forecasts dashed in the same colour, with one shared legend toggle per metric and tooltip labels distinguishing actual/forecast. Solar and household demand use kW on the left axis; predicted interval kWh are divided by actual interval hours. Stored battery energy uses kWh on the right axis; actual SOC times nameplate capacity matches the planner's initial-energy convention (rather than available-discharge energy). Actuals cover local midnight through refresh, preserve missing-minute gaps, and remain a snapshot until the next plan refresh. Forecasts come from the selected scenario. Axis labels and tooltips use the configured timezone; the detail table displays local HH:MM only; full timestamps remain in the underlying result and chart.

The horizontally scrollable detail table has no pagination. Compact view is the default and groups core columns by configured-local clock hour, including partial edge hours; Advanced exposes every scenario column at the original model resolution. Both include derived battery SOC beside stored kWh and a summed Losses column, with numeric cells and short unit-labelled headers. Header sorting is disabled, preserving chronological order. Switching detail views does not rerun the model or reset the chart. Summaries use one bordered block of inline label/value pairs. On mobile, controls wrap and summaries stack; the five-tab navigation scrolls within the viewport. Planning reads existing storage without migrations, does not persist the result and sends no device commands.

Scenario switches update the existing chart series in place, preserving legend visibility and zoom. Plan charts render without animation so complete forecasts are visible immediately after refresh.

Changing protection adjustment recomputes the plan and summaries, but does not guarantee different optimal totals. The optimiser can choose the same cost/protection outcome for several preference values. Plan rows remain in chronological order across midnight and repeated local hours; HH:MM display does not determine ordering.

The Plan detail table has a grey bold Total row, fixed below the period rows. It sums interval kWh flows/losses and import cost, export revenue and net cost from unrounded model values. Timestamps (apart from the Total label), tariffs, rates, power limits and opening/closing/minimum/maximum battery levels are blank; entirely missing energy columns remain blank.

Plan compilation, solver and total simulation timings are flushed directly to the dashboard worker's stdout, visible in the launching terminal or its redirected log.

Plan detail includes a **Plan action** column immediately after Start and End: Self-consumption, Force charge or Force discharge. The first row uses the shared next-action mode; later rows infer the action from grid-to-battery charging or battery-to-grid export above 0.000001 kWh, ignoring numerical noise. Later actions describe scenario flows, not additional solved device commands. The Total row leaves this column blank.

**Download Excel** beside Plan detail exports the selected scenario to an `.xlsx` workbook, with the currently selected Compact/Advanced columns, local HH:MM times, plan actions, SOC, rounded numeric cells and Total row as the table (in original chronological order). Hidden sorting fields are excluded. The filename includes the scenario and local plan start; the worksheet is named Plan detail. Export reuses the shared XlsxTurbo writer and does not recalculate the plan.

All dashboard table headers stay below the dashboard topbar during page scrolling and leave the viewport at the end of their own table. Shared `assets/table_headers.js` translates the real header within the table bounds on scroll/resize, preserving column widths, sorting and horizontal scrolling without a second header or an inner vertical scrollbar. Updates use animation frames; child-list observation also handles newly displayed tables and refreshed rows. Styling applies to all Quasar table headers.

`dashboard.actuals_to_forecast` maps PV strings to unique forecast array names, such as `pv1: East`. Recent live labels use these names directly; `Config.actuals_to_forecast_panel_ids` resolves them for ID-based storage queries, allocation and chart columns. This configuration change needs no database migration.

Plan table numeric values use fixed decimal places: energy, cost and power use two, tariff rates four, and SOC one. Numeric cells remain numbers and use tabular digits; totals use the same display precision. Compact hourly flows/losses/clipping/costs sum, end battery/SOC use the hour-end level, tariff rates are duration-weighted, and distinct action/tariff names are joined. Crossing intervals are split in proportion to elapsed duration, with linearly interpolated battery levels at hour boundaries. Repeated DST hours remain separate. Charts, model results and summaries retain their original resolution; Excel follows the chosen hourly Compact or original-period Advanced view.

Solar/Load scenario controls use individual bordered fieldsets with centred legends above the buttons. Unselected buttons use Quasar grey-2 with dark text; the selected button retains the primary colour and white text. The groups have a 24px gap (twice the former 12px) and wrap on narrow screens. Border/legend rules are scoped to `.plan-scenario-group`.
