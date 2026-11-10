# Saved plans and Plan History

Shared background plans are published through `solar.db`. The numerical optimisers remain database-free. Demonstrations and the as-of workbook CLI do not save shared plans.

## Ownership and publication

The supervisor owns three independently restarted services: persisted collector, headless planner and dashboard. `runtime.planner.PlannerService` watches committed readings and hot-reloads energy-plan settings. Its single `runtime.planning_worker.PlanningWorker` calculation process receives at most one job, captures its UTC input cutoff when execution begins, calculates both views, and commits a shared snapshot. The planner records health/publication status in `database_metadata.planning_service_status`.

The dashboard no longer schedules actual plans. `dashboard.plan_store.PlanStore` polls the latest saved ID and service health once per second, reads changed IDs into one shared cache, and notifies connected browsers. It restores the latest saved plan on startup. Planning continues if the dashboard is stopped. Private demonstrations use a separate, on-demand worker with one admitted request server-wide and no queue. They do not save shared plans or compete with the scheduled worker.

Both workers use `energy_plan.calculation_timeout_seconds` (default 300 seconds, including cold process startup). A timeout or process crash terminates/reaps that worker; later jobs create a replacement. Scheduler timeout detection occurs on its one-second loop. Shutdown waits are bounded. A calculation or transaction failure retains the previous published plan; a committed ID can be recovered even if a notification/readback fails. No cleanup runs without submission, and no missed snapshots are fabricated.

## One-table storage contract

Migration `0.3.3` adds `plan_history` and advances `database_metadata.database_version` atomically without rewriting telemetry or forecasts. Normal startup performs the migration; use a temporary database or a copy for development.

Each row identifies `(snapshot_id, view, scenario)`. `view` is `metadata`, `Compact`, `Advanced`, or the optional model `Expected` section. The metadata row uses an empty scenario. `saved_at` is UTC epoch **microseconds**, identifying completion time; `slot` is its integer UTC retention bucket. These differ from measurement timestamps in seconds. Indexes support time/date and slot selection.

`payload` is zlib-compressed UTF-8 JSON, never pickle. It stores a versioned report section as column names, timestamp-column names and chronological row arrays. One section row contains all intervals for a view/scenario, rather than duplicating metadata on every interval. This keeps the single table small while preserving every value needed by the UI. Numeric plan outputs retain their original floating-point precision; they are **not** scaled measurement columns. Missing values become JSON null. Interval timestamps remain full UTC ISO strings. Decoding explicitly accepts ISO8601 with mixed fractional-second precision, including precise worker cutoffs and clock-aligned later boundaries. Totals and display rounding are generated when reading/rendering, not stored as interval rows.

Metadata format version 1 includes execution start/completion, captured source reading, timezone, model, serialized energy-plan settings, the complete next action, capacity/reserve/protection settings, original simulation column/action information and visible-row boundaries. The latest report also retains its actual chart observations; these observations are removed from the previous snapshot's metadata at the next submission. Historical chart observations are read from telemetry up to the saved calculation cutoff. Each new report also retains exact scenario summaries, first-action duration/PV export and initial battery levels, so summaries remain tied to the original calculation. Existing snapshots lacking that metadata use their retained report resolution. Optional Markov expectations remain distinct from stress scenarios.

`planning.plan_report` owns the shared column definitions, period calculations, Compact hourly aggregation, Advanced labels and footer calculations. `services.plan_history.prepare_details()` runs this processing in the worker. The live UI uses these prepared views after loading from storage. Demonstrations reuse the same pure transformations without persistence. `dashboard.components.plan_detail` owns only table rendering and Excel output.

## Retention on successful submission

The entire policy is configured under `energy_plan.history`:

```yaml
history:
  retain_snapshots_frequency_mins: 30
  retain_snapshots_period_days: 30
```

Both values are positive integers and default to 30 when omitted. Keep the first successful snapshot in each configured UTC interval, with all scenarios and both Compact and Advanced views (including optional model Expected sections), for the configured number of elapsed days. Always protect the latest submitted plan, even if it is outside the retained slot or the application has been idle beyond the period. Superseded intermediate plans are removed at the next successful submission; there is no recent-every-run tier or separate Advanced expiry.

`slot = saved_at // (frequency_mins * 60_000_000)`. A frequency change re-buckets surviving rows atomically before selecting keepers; `database_metadata.plan_history_frequency_mins` records the current bucketing interval. Changing the period/frequency cannot recover snapshots or Advanced data already deleted under an earlier policy. The planner hot-reloads these settings with energy_plan; a save using the new settings applies the new policy. No read or configuration edit itself deletes history.

First-in-slot selection uses completion timestamp then snapshot ID for deterministic ties. Age cutoffs use elapsed UTC time through daylight-saving transitions. If no submission occurs for a week, old data remains until the next successful save catches up. SQLite reuses deleted space; cleanup does not run VACUUM or promise immediate file shrinkage. Retention does not load the archive into application RAM: live plans are shared-cached, and historical reports are loaded on demand.

## UI

There is a single **Plan** tab with the existing action, summaries, scenario controls, chart, Compact/Advanced table and Excel export. Immediately right of **Plan settings** are **Plan date** and **Snapshot** controls. Both start blank: the page follows the latest shared actual plan. A date filters the available snapshot times but does not itself select a historical plan. Selecting a snapshot pins that report; new shared publications do not replace it. Clearing the snapshot or date resumes latest. Async selection requests are versioned so late reads cannot replace a newer selection. Completed demonstrations also cannot replace a pinned historical selection.

Historical reports use their captured settings, first action and exact saved summaries. Retained snapshots offer their original Advanced resolution throughout the retention period. Legacy Compact-only snapshots from an earlier policy still display their saved hourly data and explain why Advanced is unavailable. Actual chart observations are queried from telemetry at or before the original run cutoff. These observations can reflect later historical telemetry corrections; the forecast remains the saved one. Chart legend/zoom and scenario selections survive switches. Neither history selection nor export runs the optimiser.

Date bounds use successive configured-local midnights, preserving 23/25-hour days. Snapshot times include timezone labels for repeated hours. Tables preserve chronological UTC order, existing formatting and totals. Desktop controls remain to the right of settings; on mobile they wrap in the same order within the viewport.

## Verification

Targeted storage/dashboard tests cover precision, retention, rollback, original summaries in historical reports, cache publication and private demonstration admission. `tests/runtime/test_planner.py` runs real spawned calculation processes, injected synthetic collection/planning processes, deadlines, crashes, restarts, Windows descendant cleanup and actual supervisor recovery from a dashboard crash. `tests/browser/test_plan_history.py` exercises a real browser with concurrent synthetic collector/planner processes: blank defaults, control placement, historical pinning during new publications, chart legend preservation, clearing to latest, historical views, Excel and desktop/mobile widths. `test_plan_tab.py` retains demonstration/settings/rendering checks. These use temporary databases and synthetic measurements/reports. A separate production-worker regression assembles temporary device/history/forecast inputs, executes the real MILP optimiser inside the spawned worker and verifies all nine views after save/readback; live device/provider integration remains separate.
