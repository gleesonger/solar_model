# System review plan

Prepared **2026-10-15**. **Status: review report complete; verification gaps remain explicitly recorded.** Implementation fixes remain outside the approved review scope. See the [report](system-review-report.md), [coverage matrix](system-review-coverage.md) and [recommended work](system-review-recommendations.md).

## 1. Purpose and priorities

Establish whether the system collects, stores, processes and presents trustworthy information; whether all website features work; and where consolidation would materially improve maintainability.

Priorities, in order:

1. Incorrect or lost data, misleading browser outputs and invalid forecast evaluation.
2. Failures, recovery weaknesses and operational exposure.
3. Performance, resource use and concurrency.
4. Duplicated responsibilities, inconsistent conventions and documentation gaps.

Report material, evidenced issues. Avoid speculative defects, cosmetic complaints and abstraction proposals without a practical benefit.

## 2. Scope and working boundaries

- Inspect active production entry points, supporting modules, configuration, database schema/migrations, dashboard Python/JavaScript/CSS, tests, deployment files and documentation.
- Include planning, research and maintenance scripts where their calculations, dependencies or database writes affect understanding or correctness. Distinguish these from production behavior.
- Read the [documentation index](../index.md), [architecture](../overview/architecture.md), [implementation reference](../implementation/index.md) and root [AGENTS.md](../../AGENTS.md) first. Verify documentation against source rather than treating it as proof.
- Record the reviewed revision, relevant working-tree changes, dependency versions and test environment. Preserve unrelated work.
- Use temporary databases or SQLite backups. Do not run migrations, backfills, tariff recalculation, restarts or destructive tests against production.
- Exercise operational Control actions only in an isolated instance with test services or mocks. Verify action wiring and confirmation behavior without issuing real device commands or restarting the user's application.
- Use deterministic provider/device responses for automated tests. State any live hardware/provider behavior that remains unverified.
- Produce findings and recommendations; do not refactor, change application behavior or apply fixes during the review. Separate proposed corrections from the evidence supporting them.

## 3. Review sequence

### Independent reviewers

Use three fresh review agents, each started **without conversation history or inherited prior analysis**. Supply only this plan, repository access, the workspace location and operating constraints. Each agent reads the code and documentation and forms its own model of the system. Fresh context reduces inherited assumptions; findings still require evidence.

| Reviewer | Primary responsibility |
| --- | --- |
| Data correctness | Collection, persistence, units/timestamps, calculations, forecasts, planning and independent expected results |
| Website verification | Complete feature inventory, browser workflows, rendered outputs, downloads, responsive behavior and independent expected results |
| Architecture and consistency | Runtime, concurrency, recovery, configuration/security, performance, duplicated logic and overlapping libraries |

- Each reviewer covers the relevant workstreams below; responsibilities divide work without removing any scope. The coordinator reconciles ownership of cross-cutting checks and gaps.
- Keep initial assessments and findings separate until each reviewer submits its first assessment. Do not seed reviewers with the coordinator's conclusions or another reviewer's suspected defects. Repository documentation and existing findings remain available, but must be checked against evidence.
- Use a separate temporary database, browser profile/instance, service port and artifact location for each reviewer where needed. Prevent production connections and interference between tests. Run resource-intensive benchmarks sequentially for useful timing comparisons.
- Reviewers may create isolated test fixtures and evidence artifacts; they must not modify application code or shared documentation while conducting their independent assessments. Submit results to separate reviewer artifacts.
- Each reviewer supplies coverage statuses, reproducible evidence, recommendations and limitations. Independently calculate expectations rather than accepting application output or another reviewer's calculations as ground truth.
- After initial submission, the coordinator compares reports, resolves contradictions, reproduces material findings and distinguishes intentional semantic differences from defects. Request targeted cross-checks where useful.
- Merge validated findings into one prioritized report and coverage matrix. Deduplicate overlapping findings, preserve relevant minority concerns as unresolved questions, and make unverified claims explicit.

The coordinator owns overall coverage and final assessment. Creating or amending this plan does not start these agents; launch them only when review execution is approved.

### Execution stages

| Stage | Work | Required output |
| --- | --- | --- |
| Baseline | Identify active processes, code paths, dependencies, test prerequisites and existing findings | Environment record and limitations |
| Inventory | Enumerate subsystems, website features, controls and output families from source and the running isolated website | System map and coverage matrix |
| Trace | Follow telemetry, forecasts, tariffs and derived quantities through every boundary | Data contracts and calculation checks |
| Verify | Run focused tests, independent calculations, browser workflows and representative performance checks | Reproducible evidence and coverage statuses |
| Consolidate | Compare repeated logic, libraries and responsibilities across modules | Ranked consolidation opportunities |
| Assess | Confirm suspected defects, check intentional differences and evaluate practical impact | Prioritized findings and recommended work |

Confirm the feature inventory before declaring coverage complete. Add features discovered during execution; the examples below are a starting inventory, not a fixed ceiling.

## 4. Technical review workstreams

### Architecture, runtime and recovery

- Trace `solar_model/runtime/supervisor.py`, `solar_model/runtime/collector.py` and `solar_model/dashboard/app.py`, including standalone startup and database initialization.
- Check scheduling, worker/thread ownership, shared versus browser-specific state, shutdown and restart behavior.
- Verify exception handling, partial success, retry behavior, stale-state retention and recovery after failures.
- Exercise interrupted collection, process restarts, cache rebuilding and failed/interrupted migrations on temporary databases. Verify that recovery and repeated operations do not duplicate records, apply transformations twice or leave partial/corrupt state.
- Check concurrent browsers, overlapping requests, collector/dashboard database access and out-of-order responses.
- Use two independent browser sessions to verify state isolation: one session's filters, chart selections, custom charts and downloads must not alter another session's selections or results. Shared live collectors and snapshots should remain shared only as intended.
- Distinguish persisted collection from live polling, and hypothetical planning from operational control.

### Collection and persistence

- Check Modbus register decoding, signed values, scaling, units and unavailable/invalid readings.
- Check Forecast.Solar request/response handling, due-time scheduling, timeouts, retries and partial responses.
- Trace input records through validation, derived measurements, transactions and stored rows.
- Check duplicate timestamps, interrupted writes, restart behavior, missed intervals and database-lock failures.
- Verify schema/migrations, timestamp representations, indexes, retention and backfill behavior on temporary databases.
- Confirm fixed-point integer storage versus physical-unit ORM values; inspect every raw SQL conversion along reviewed paths.

### Calculations, dates and aggregation

- Verify energy, power, cumulative counters, rates, costs, revenue and battery quantities against their intended definitions.
- Check counter resets, first observations, independent null patterns, zero values, negative values and irregular sampling.
- Check sample-weighted versus interval-weighted means, partial periods, totals and Sum/Average semantics.
- Check configured timezone, UTC conversion, inclusive/exclusive bounds, midnight, week/month/year/season boundaries, DST and non-hour timezone offsets.
- Compare raw-sample and rollup calculations. Establish when their semantics should match and when differences are intentional.

### Forecasting, planning and research

- Verify collection/snapshot time versus target time, earliest/latest/as-of selection and preservation of raw forecast history.
- Trace raw, corrected and beginning-of-day forecasts; check panel allocation, units and cumulative-to-period conversion.
- Check training/evaluation cutoffs, unavailable future information, missing predictions and historical reproducibility.
- Review planning balance equations, battery capacity/efficiency/power constraints, tariff assumptions and boundary conditions.
- Identify differences between experimental models and the production model; do not classify intentional research differences as defects.
- Confirm that planning/report generation does not imply an implemented inverter controller.

### Performance, configuration and security

- Inspect bounded versus lifetime scans, query plans/index use, ORM materialization, repeated queries and Python work on source rows.
- Check cache invalidation, refresh cadence, request races, event-loop blocking and memory retention.
- Benchmark representative small/large requests where needed, separating query, transformation, serialization and browser rendering/transfer.
- Reuse [existing download measurements](download-performance.md) as context; label writer versions and avoid treating historical figures as current guarantees.
- Review configuration validation, invalid defaults, relative paths, dependency compatibility, secrets/logging, external input and network exposure.
- Assess dependency maintenance, licensing, supported Python/platform versions and deployment compatibility. For native dependencies such as XlsxTurbo, check wheel availability and installation in the project's target environments. Report concrete incompatibilities and evidenced risks rather than treating a dependency's age or popularity alone as a defect.
- Assess authentication/authorization requirements against the intended deployment; distinguish confirmed exposure from unknown deployment assumptions.

## 5. Website functionality review

Inventory all mounted pages/tabs and enumerate their individual controls, interactions and outputs. Keep dormant components such as the unmounted Hourly tab separate from user-facing coverage.

| Feature family | Functional checks |
| --- | --- |
| Recent | Live/Today readings, forecast summaries, past-week/month tables, totals and refresh behavior |
| Analysis | Every built-in energy, power, battery, panel and money chart; date ranges, grouping and Sum/Average controls |
| Custom charts | Available fields, valid/invalid selections, chart generation, updates and empty results |
| Chart interactions | Legend selection, chart/table toggles, zoom where supported, tooltips, clipboard and chart-specific CSV |
| Full-data downloads | CSV/XLSX, forecasts on/off, every timestep, date validation, filenames, errors and retry behavior |
| System Info | Metadata selection, missing values, refresh and presentation |
| Control | Confirmation/cancellation, isolated action execution, progress, success/failure and notification behavior |
| Connection/freshness | Offline, socket loss, missing heartbeat, stale persisted/live data, reconnect and recovery |
| Layout | Desktop and mobile, navigation, table overflow, chart sizing, readable labels and usable controls |

For each workflow:

- Confirm that controls trigger the intended action and that the visible result corresponds to the current selection.
- Check initial, loading, successful, empty, failed and recovered states where applicable.
- Test rapid changes and repeated actions for stale results, duplicate work or inconsistent state.
- Capture relevant browser console errors, failed network requests and server exceptions; connect them to observed impact.
- Inspect final rendering as well as browser chart/table data. A successful server response does not prove the browser displayed it correctly.

## 6. Independent browser-output verification

### Establish expected results

- Record intended behavior and its basis before judging correctness. Where code, documentation, UI or available requirements disagree, describe the conflict and request direction if it materially affects the conclusion; do not silently select one as authoritative. Keep unresolved expectations separate from confirmed defects and continue checks that do not depend on them.
- Create a small, controlled database whose readings, forecasts, tariffs and expected outputs can be explained explicitly.
- Derive expectations using hand calculations or a separate reference implementation. Do not call the application aggregation/query functions to produce expected values.
- Start from known raw units, timestamps and field meanings. Independently calculate scaling, period allocation, forecast selection, costs and totals.
- Define tolerances per quantity before comparison. Check unrounded values and displayed rounding separately; explain any permitted differences.
- Use a real-data backup for representative spot checks and scale, while retaining deterministic fixtures for interpretable edge cases.

### Verify each output family

| Output | Required comparison |
| --- | --- |
| Live/summary values | Source reading, derived value, unit, freshness and blank/stale behavior |
| Charts | Series identity, every point in controlled fixtures, timestamps/order, signs, axes, units, tooltips and visual rendering |
| Tables | Rows, labels, ordering, formatting, totals and correspondence with the selected chart/filter |
| Forecast outputs | Chosen vintage, target interval, raw/corrected interpretation and actual-versus-forecast alignment |
| Economics | Tariff band, rate, import cost, export revenue, net values and applicable totals |
| Downloads/clipboard | Schema, row count, field labels, values, dates/timezones, nulls, numeric/date types and format-specific behavior |
| Notifications | Correct trigger, message, priority, persistence and recovery |

Compare outputs only where their contracts should agree. Full-data exports aggregate stored fields; chart forecast-energy differences and rollup paths can legitimately have different semantics. Document those differences rather than forcing numerical equality.

### Required scenarios

- Normal representative history and a small dataset with independently known results.
- Empty database, one sample, missing fields, missing forecasts and forecast-only/actual-only periods.
- Multiple forecast collections per day, overlapping targets and incomplete panel data.
- Cumulative counter resets/decreases, duplicate/restarted collection and irregular gaps.
- Midnight and calendar boundaries, leap day, spring/fall DST and a non-hour timezone offset.
- Tariff transitions and changes within an aggregation interval; Sum/Average and totals with partial periods.
- Collection/provider/database failures, stale snapshots, disconnected browsers and successful recovery.
- Large selections, repeated downloads and multiple browsers where concurrency affects behavior.

Cover every distinct feature, output type and calculation path. Exhaustive testing of every possible input/control combination is not claimed; record the combinations exercised and any residual gaps.

## 7. Code consistency and consolidation

| Check | Questions |
| --- | --- |
| Duplicated calculations | Are energy, tariffs, forecast differences, counter resets or totals implemented repeatedly? Can they diverge? |
| Duplicated data access | Do SQL builders, ORM loaders, rollup queries and maintenance scripts repeat the same responsibilities? |
| Date/unit/null handling | Are timezone conversion, bucketing, fixed-point scaling and missing-value rules consistently implemented? |
| Overlapping libraries | Which libraries solve the same problem? Are differences justified by writing/reading, performance, compatibility or script-specific needs? |
| Shared contracts | Would common field definitions, typed records, validation or query interfaces prevent errors? |
| UI conventions | Are labels, formatting, chart construction, notifications and state handling consistent? |
| Operational conventions | Are configuration access, sessions, logging, exceptions and resource cleanup consistent? |
| Legacy paths | Which wrappers/helpers are active, test references, experiments or unused implementations? |

For each material opportunity, identify the locations, callers, semantic differences, proposed shared responsibility, expected benefit, migration risk and necessary regression coverage.

Do not consolidate merely because code looks similar. Preserve distinctions between live/persisted data, raw/rollup aggregation, forecast vintages, chart/export contracts and production/research models. A recommendation may be to clarify or isolate intentional duplication rather than remove it.

## 8. Coverage and evidence records

Use one row per meaningful feature/output/scenario. Split rows when different calculation paths or controls require separate evidence.

| ID | Feature/output and scenario | Interaction works | Values verified | Edge cases checked | Evidence/issues |
| --- | --- | --- | --- | --- | --- |
| To populate during review | | Not checked | Not checked | Not checked | |

Statuses: **Pass**, **Fail**, **Partial**, **Blocked**, **Not checked**, or **Not applicable** with a reason. Include browser viewport and fixture identity when relevant. Server-only verification must not be marked as browser verification.

Evidence should be sufficient to reproduce the conclusion: code location, fixture/input, action, expected/actual values, relevant logs/query results and browser artifacts. Keep large/sensitive raw artifacts out of Markdown; link to appropriate local artifacts and summarize the material evidence.

Existing test success is supporting evidence, not proof of complete coverage. Record the full test command, failures, skips and excluded modules. The missing migration-module import documented in [development and testing](../operations/development-and-testing.md) must remain explicit if still present; do not label the temporary subset as a full-suite pass.

## 9. Findings and recommendations

Each finding should contain:

- Category: correctness, reliability, performance, security, maintainability or documentation.
- Priority and confidence, with the concrete reason for each.
- Affected files/functions and user-visible or operational consequence.
- Trigger/input, expected behavior, actual behavior and reproducible evidence.
- Recommended correction, likely scope, risks and validation needed.
- Concrete acceptance criteria for each proposed fix: the scenario to rerun, expected observable result and applicable numeric tolerance or recovery condition. Recommendations should be verifiable without requiring the reader to infer what "fixed" means.

Priorities:

| Priority | Meaning |
| --- | --- |
| Critical | Demonstrated severe data loss, dangerous behavior or serious exposure requiring immediate attention |
| High | Material incorrect results, unavailable core features or unreliable recovery |
| Medium | Bounded correctness/reliability issues, substantial performance cost or recurring maintenance risk |
| Low | Useful consolidation/documentation improvements with limited immediate impact |

Separate confirmed defects, consolidation opportunities and unresolved questions. Check existing [findings](findings.md) before reporting duplicates. Reproduce suspected defects and verify intended behavior before assigning severity; do not overstate untested concerns.

## 10. Deliverables and completion criteria

Produce concise Markdown under `documentation/reviews/`, linked from the [documentation index](../index.md):

- **Review report:** baseline, key conclusions, prioritized findings and explicit limitations.
- **Coverage matrix:** every inventoried website feature/output family, tested scenarios, statuses and evidence.
- **Recommended work:** essential fixes, worthwhile improvements and optional consolidation, with dependencies and verification requirements.

Retain the three independent initial assessments as supporting evidence. The final report must distinguish reviewer observations from coordinator-validated conclusions and identify any unresolved disagreement affecting the recommendations.

Keep detailed evidence beside the relevant finding or in linked artifacts rather than repeating it across documents. Choose final report filenames when execution starts; do not create empty result documents now.

The review is complete when the inventory has been reconciled with source and the isolated website, every scoped area has a recorded status, material findings have been assessed, and unverified behavior is explicit. Blocked areas require a stated cause and next step; they must not silently count as passing.

The final conclusions must prominently state material coverage gaps and unresolved expectations. Few or no confirmed findings must not imply that blocked, partial or untested areas passed; distinguish completion of the review report from complete verification of the system.

The user approved execution of this review on 2026-10-15. Record results and coverage separately from this plan; implementation fixes require a subsequent task.
