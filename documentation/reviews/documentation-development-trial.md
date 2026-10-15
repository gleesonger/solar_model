# Documentation-only development trial

## Purpose and method

On 2026-10-14, a fresh **Sol 6.1 agent, Medium reasoning**, was started with `fork_turns="none"`. It received no conversation history. The trial tested whether the documentation supports useful development proposals before source inspection.

1. The agent read root `AGENTS.md` and all 23 Markdown pages then present under `documentation/`, forming a mental model before receiving feature requests.
2. It proposed four hypothetical changes using that documentation and supplied feature requirements only. Source links could not be followed; source, tests, configuration, register maps, Git/history, data, and vendor PDF were excluded.
3. The parent compared the frozen proposals with actual source and identified targeted follow-up questions.
4. Only then was the same agent allowed to inspect relevant source to distinguish missing documentation from omissions in its proposal or entirely new design requirements.

Both documentation-only phases included access attestations. The original proposals remained unchanged during source review. This is a qualitative high-level design trial, not proof that every implementation detail is documented or that documentation-only coding would succeed.

No proposed feature was implemented. No project code, tests, hardware requests, or database operations were executed for the trial. The only changes were this report, navigation, and the small documentation addition described below.

## Mental-model result

Before seeing development scenarios, the agent correctly identified:

- Separate supervisor, persisted collector, dashboard snapshot worker, browser-activated live collector, and per-browser state.
- Physical-unit decoder tuples, fixed-point storage, ORM/raw-SQL conversion, nullable counter deltas, and atomic sample/rollup writes.
- Collection versus target time, midnight-anchored forecast source versus current destination vintage, scalar/residual production correction, and separate energy integration.
- Offline exact-weekday load profile, hierarchical weekday/weekend experiment, and tree-model research as distinct implementations.
- Cutoff-selected planning inputs, configuration-dependent reproducibility, independent scenarios, unenforced metadata limits, and no current actuation.
- Mounted Analysis SQL versus older aggregation helpers; shared snapshots versus atomic chart-query batches; live allocations versus persisted meter values.
- Documented timezone, forecast-interval, partial-vintage, replay, and test-coverage limitations without treating them as intended policy.

This model agreed with the source inspected by the parent. The agent did not mistake experimental code for an existing runtime load service or controller.

## Scenario A: separate battery charge/discharge charts

**Request:** Add standard Analysis charge/discharge energy charts with tariff-band comparison, expose both metrics to custom charts, and retain grouping, controls, and mobile behavior.

**Documentation-only proposal:** Reuse existing plant battery charge/discharge `_period` fields and hourly data. Add explicitly new chart metrics to shared SQL and Analysis energy requests, preserve the net-flow series, use `ChartDataDisplay`, and extend custom choices/labels. Treat tariff band as a new supported split dimension, not an existing calendar split or evidence of battery-energy origin.

**Source assessment: sound architecture; small lifecycle gap.**

- [database.py](../../database.py) already declares the required period meters and mirrors them into hourly rows.
- [chart_data_sql.py](../../dashboard/chart_data_sql.py)'s `ACTUAL_COLUMNS` and `_build_plan()` are the correct query extension points; band identity is currently lost in ordinary hourly actual grouping, so new band splits need deliberate SQL changes.
- [data.py](../../dashboard/data.py)'s `load_analysis_range()` and custom metric list are the active adapter/selection path.
- [anlaysis.py](../../dashboard/anlaysis.py), [charts.py](../../dashboard/charts.py), and [ChartDataDisplay](../../dashboard/chart_display.py) are the correct presentation path.
- Current split validation accepts calendar dimensions only. The agent correctly labelled tariff splitting as new rather than claiming it works already.

The initial proposal did not explicitly include the fixed chart record and per-chart refresh registration. Phase 3 confirmed the necessary `HistoricalCharts` field, construction/return, and `_update_historical_charts()` edits; see the documentation improvement below.

## Scenario B: collect inverter temperature

**Request:** Persist temperature on full collection, add historical display, optionally expose live values; device register specifications are supplied separately.

**Documentation-only proposal:** Add the verified map definition and declared ORM field, keep physical/scaled units distinct, use hourly sums with temperature-appropriate averages, extend telemetry query/frame/chart paths, and independently extend the live metric filter/value contract if live display is chosen. It flagged unit naming, missing-value denominators, and whole-interval failure semantics as decisions to check.

**Source assessment: sound architecture; same chart lifecycle gap.**

- [modbus_collector.py](../../modbus_collector.py)'s map loading/decoding feeds full electrical reads; no extra persisted scheduler is needed.
- `SolarDatabase.save_modbus_sample()` rejects undeclared metrics, so adding only a register definition would fail. The proposed ORM extension is necessary.
- `HOURLY_MEASUREMENT_COLUMNS` and `SigenStorHourlySample` derive from raw model declarations; new non-cumulative numeric fields use the sum convention automatically. Historical added columns remain null without a separate backfill.
- [chart_data_sql.py](../../dashboard/chart_data_sql.py)'s telemetry mapping and explicit mean path are appropriate. Additional valid-reading counts would be a new design if partial temperature missingness must be supported.
- [live.py](../../dashboard/live.py)'s filtered map and values transformation are separate from persisted collection, as the agent recognised.

No temperature field, register address, sentinel rule, or device capability was invented. A new historical chart still needs explicit reference/refresh wiring, as in A.

## Scenario C: runtime load forecasting

**Request:** Issue reproducible 24-hour hourly-kWh forecasts, retain issue/target/model identity, show observed versus forecast load, and make forecasts available to planning.

**Documentation-only proposal:** Extract the planner's exact weekday/hour baseline into a clearly new module, keep workbook compatibility, add an independently supervised runtime service, and introduce issue/interval storage with atomic complete issues and reproducible training/settings identity. Select complete cutoff-eligible issues for UI/planning rather than mixing vintages or loading offline tree artifacts.

**Source assessment: sound extension, with new policy decisions.**

- [test_energy_plan_as_of.py](../../test_energy_plan_as_of.py)'s `load_weighted_profile()` is the stated baseline and can be extracted without invoking the workbook CLI or its output deletion.
- The existing runtime has no persisted load-forecast service/table. The agent correctly proposed new components rather than assuming the report experiment is already production.
- [docker_start.py](../../docker_start.py) explicitly starts two children; adding another process requires the proposed supervisor/configuration changes.
- New tables require declarations, writers, and inclusion in `open_database()`'s explicit creation/migration path. Existing forecast selection/storage is solar-specific.
- Planner integration through explicit issue selection and `ForecastInterval` construction is compatible with current solver inputs.

UTC issue/target boundaries, generation time, versioned settings/training input identity, DST handling, incomplete histories, and model fallback remain choices for the new feature. They are not absent descriptions of an existing runtime service.

## Scenario D: dynamic controller

**Request:** Replan every 15 minutes using solar/load forecasts, first in advisory/simulation mode, with a later verified actuation adapter.

**Documentation-only proposal:** Add a sequential supervised controller, shared explicit interval adapters, independent freshness/coverage checks, one base-scenario solve for one actionable first interval, loss-aware energy-to-power translation, decision/input auditing, and a separate future actuator contract. Do not depend on browser-activated live state or treat independent scenario reports as one common plan.

**Source assessment: sound architecture; initial C/D coverage combination incomplete.**

- [energy_plan_simulation.py](../../energy_plan_simulation.py)'s `simulate_energy_plan()` accepts a single named scenario and arbitrary covered projection start; using the first interval supports the proposed receding-horizon design.
- Its energy routes and efficiency losses require deliberate command translation. Independent scenarios have no shared near-term decision, as the agent recognised.
- [live.py](../../dashboard/live.py) pauses without connected browsers; it is not an appropriate sole state source for an independent controller.
- Current Modbus code performs reads only. A write/acknowledgement/expiry adapter is genuinely new and needs verified specifications.
- Documented forecast start/end disagreement and SOC/capacity assumptions must be resolved before a control implementation relies on them.

The initial hourly issue design in C covers only `[10:00 today,10:00 tomorrow)`. A full 24-hour solve at 10:15 requires another 15 minutes of forecast. An acceptable age alone does not establish coverage.

Phase 3 confirmed `ForecastData._sum_intervals()` permits partial overlap but raises for uncovered tails, and `simulate_energy_plan()` requests the full configured horizon. Existing docs explain this correctly. The omission was in the proposed feature combination: choose extra guard coverage, more frequent issues with sufficient horizons, or an explicitly shortened controller horizon. Generation delays/missed issues also need a policy.

## Documentation improvement made after the trial

The original [dashboard presentation guide](../implementation/dashboard-ui.md) explained lazy chart creation, Apply/update flow, and component behavior but did not enumerate the fixed `HistoricalCharts` record or the three chart-registration steps.

Phase 3 established the exact sequence:

1. Add a `ChartDataDisplay` field to [models.py](../../dashboard/models.py)'s `HistoricalCharts`.
2. Construct and return it from `charts.render_historical_charts()`.
3. Update it explicitly in `AnalysisTab._update_historical_charts()`, with an options callback using the newly supplied DataFrame.

That concise section is now added to the guide. The agent acknowledged that omitting `_update_historical_charts()` from its initial proposal was partly its own omission, since the documented call flow already named it. Missing fixed-record structure was the concrete documentation gap.

## Conclusion and limits

All four proposals located the correct architectural boundaries and major change paths. The trial found one specific structural documentation gap, which was filled, and one new-design compatibility omission, which was recorded without blaming the documentation.

This supports the documentation's usefulness for independent high-level development planning. It does not justify claiming it is exhaustive or bypassing source review during implementation. Hardware write specifications and newly chosen service/controller policies remain outside the current program's documented contract.
