# Saved plans and Plan History

Shared background plans are published through `solar.db`. The numerical optimisers remain database-free. Demonstrations and the as-of workbook CLI do not save shared plans.

## Ownership and publication

1. `dashboard.plan_store.PlanStore` schedules one calculation through the existing persistent process worker.
2. `services.plan_history.generate_saved_plan()` calls `generate_plan_report()` and prepares Compact and Advanced data for every scenario using `planning.plan_report`.
3. `storage.plan_history.save_plan()` inserts the complete snapshot and performs retention under one SQLite `BEGIN IMMEDIATE` transaction. It returns a snapshot ID only after commit.
4. The server reads that ID with `read_saved_run()` into its shared cache, then publishes its existing notification. Browser clients consume the same loaded report; they do not each query the database.
5. At startup the server restores the latest committed plan, including its original settings, source-reading timestamp, action, actual chart observations and full Advanced data. It still waits for a new full reading before calculating again. Reconnecting browsers receive the shared cached version.

A calculation, write, cleanup or readback failure leaves the previous published plan in place and records an error. A committed plan remains recoverable on restart even if notification or readback failed. There is no separate cleanup timer. No new plans are generated to fill downtime.

## One-table storage contract

Migration `0.3.3` adds `plan_history` and advances `database_metadata.database_version` atomically without rewriting telemetry or forecasts. Normal startup performs the migration; use a temporary database or a copy for development.

Each row identifies `(snapshot_id, view, scenario)`. `view` is `metadata`, `Compact`, `Advanced`, or the optional model `Expected` section. The metadata row uses an empty scenario. `saved_at` is UTC epoch **microseconds**, identifying completion time; `slot` is its integer UTC half-hour bucket. These differ from measurement timestamps in seconds. Indexes support time/date and slot selection.

`payload` is zlib-compressed UTF-8 JSON, never pickle. It stores a versioned report section as column names, timestamp-column names and chronological row arrays. One section row contains all intervals for a view/scenario, rather than duplicating metadata on every interval. This keeps the single table small while preserving every value needed by the UI. Numeric plan outputs retain their original floating-point precision; they are **not** scaled measurement columns. Missing values become JSON null. Interval timestamps remain full UTC ISO strings. Totals and display rounding are generated when reading/rendering, not stored as interval rows.

Metadata format version 1 includes execution start/completion, captured source reading, timezone, model, serialized energy-plan settings, the complete next action, capacity/reserve/protection settings, original simulation column/action information and visible-row boundaries. The latest report also retains its actual chart observations; these observations are removed from the previous snapshot's metadata at the next submission. Historical browsing concerns the saved plan tables, not a frozen telemetry chart. Optional Markov expectations remain distinct from stress scenarios.

`planning.plan_report` owns the shared column definitions, period calculations, Compact hourly aggregation, Advanced labels and footer calculations. `services.plan_history.prepare_details()` runs this processing in the worker. The live UI uses these prepared views after loading from storage. Demonstrations reuse the same pure transformations without persistence. `dashboard.components.plan_detail` owns only table rendering and Excel output.

## Retention on successful submission

| Age at submission | Data retained |
| --- | --- |
| Up to and including 30 minutes | Every successful shared run, both views and all scenarios |
| Over 30 minutes through 24 hours | First successful snapshot in each UTC half-hour slot, both views |
| Over 24 hours through 30 days | Same slot snapshots, Compact only |
| Over 30 days | Removed |

The latest submitted snapshot is always protected, including its Advanced data. Slots use completion timestamps, so a calculation finishing after a boundary belongs to that completion slot. First-in-slot selection uses timestamp then snapshot ID for deterministic ties. Age cutoffs use elapsed UTC time, including through daylight-saving transitions. Reads do not prune data. If no submission occurs for a week, old data remains until the next successful submission catches up. SQLite can reuse deleted space; cleanup does not run VACUUM or promise that the file immediately shrinks.

## UI

The new **Plan History** tab loads only when activated. It offers a local-date selector, saved snapshot time (with timezone), Latest, Previous/Next within the selected date, Solar/House load scenario selectors and Compact/Advanced views. Default scenario is medium/medium. Calculated time, captured source reading and model identify the selected result. Older snapshots explain that Advanced details have expired. Empty dates, failed reads and loading have explicit states. Excel exports the selected saved scenario/view using the same formatting and totals as the live Plan table. Neither browsing nor exporting runs the optimiser.

Date bounds use successive local midnights, preserving 23/25-hour days. Stored interval order is chronological across midnight and repeated local hours. The table keeps the existing numeric formatting, horizontal scrolling, blue headers and footer styles. Scenario/view selections change only the displayed saved data. No browser chart is added to the history tab.

## Verification

Targeted tests live in `tests/storage/test_plan_history.py`, `tests/dashboard/test_plan_history.py` and `tests/browser/test_plan_history.py`. They exercise report precision and all scenarios, off-hour grouping, retention, rollback, UTC slot identities, migration without telemetry rebuild, publish-after-commit, restart restoration, private demonstrations, desktop/mobile navigation, expired Advanced details, empty states and Excel downloads. Tests use synthetic results and temporary databases, without model simulations or the installation database.
