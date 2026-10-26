# Code map

The application lives in the installable `solar_model` package. The root `docker_start.py`, `main_data_collection.py`, `main_dashboard.py` and `recalculate_economics.py` files are compatibility launchers. Configuration and database paths still resolve from the working directory.

## Package layout

```text
solar_model/
  config.py, tariffs.py, time_utils.py, logging_setup.py, paths.py, utils.py
  planning/
    types.py, scenarios.py, optimisation.py, system.py
    forecasts/solar.py, house_load.py
  services/collection.py, forecasting.py, planning.py
  integrations/
    forecast_solar.py
    sigenstor/client.py, registers.py, datasheets.py, register_maps/
  storage/
    schema.py, connection.py, migrations.py, measurements.py, forecasts.py
    queries/planning.py, forecasting.py, dashboard.py, recent.py, analysis.py, exports.py, types.py
  dashboard/
    app.py, layout.py, state.py, snapshot.py, live.py, formatting.py
    tabs/recent.py, analysis.py, system_info.py, control.py
    components/charts.py, tables.py, chart_display.py, download_dialog.py, warnings.py
    assets/
  runtime/supervisor.py, collector.py
  tools/plan_as_of.py, reports.py, evaluate_forecasts.py, evaluate_load.py,
        rebuild_forecasts.py, recalculate_economics.py
```

## Responsibility boundaries

| Area | Responsibility and entry points |
| --- | --- |
| [Planning simulation](../../solar_model/planning/optimisation.py) | `simulate_energy_plan`: in-memory device/state/scenarios to `SimulationResult`. The service creates scenarios directly; planning has no storage/network/UI imports |
| [Forecast models](../../solar_model/planning/forecasts/solar.py) | Pure solar day-ahead/intraday calculations; [house_load.py](../../solar_model/planning/forecasts/house_load.py) predicts the fixed daily load profile |
| [Optimisation](../../solar_model/planning/optimisation.py) | CVXPY flow variables, constraints, HiGHS solve and energy-balance checks; [system.py](../../solar_model/planning/system.py) derives physical limits; the standalone `score_simulation` in this script builds the agreed reference-normalised cost/protection objective |
| [Services](../../solar_model/services) | Collection transactions, forecast scheduling/calibration persistence, and as-of planning input assembly |
| [Integrations](../../solar_model/integrations) | HTTP provider client; Modbus transport, map parsing/decoding and device specifications. Register-map JSON is packaged beside its loader |
| [Storage](../../solar_model/storage) | Schema and scaled types; engine/session ownership; migrations; measurement/forecast transactions. `SolarDatabase` composes the two storage mixins |
| [Queries](../../solar_model/storage/queries) | Planning/forecast input selection, native Recent/Analysis/export SQL, dataframe adapters and data-only contracts. No NiceGUI dependency |
| [Dashboard](../../solar_model/dashboard) | NiceGUI lifecycle, browser state, live polling, shared snapshots, tabs, reusable widgets and formatting. `analysis.py` replaces the old misspelt module |
| [Runtime](../../solar_model/runtime) | Managed child lifecycle and persisted collection loop; subprocess imports work from a separate configuration directory |
| [Tools](../../solar_model/tools) | Explicit module CLIs and workbook output, separate from normal application startup |

The unmounted Hourly tab was removed. Older ORM/pandas query paths remain where they support current calculations or independent SQL regression comparisons; they are not a second UI. The independent `check-tariffs/` experiment retains its inputs and reports; it is outside application startup.

## Data flow

```text
runtime.collector → integrations → services.collection / services.forecasting → storage
storage.queries → dashboard.snapshot / dashboard.tabs → components / formatting
 tools.plan_as_of → services.planning → storage.queries.planning
                                    -> scenarios -> optimisation -> reports
```

Production forecasting and offline planning share the house-load prediction helper and train on integrated House Load; planning limits observations to those before its as-of local midnight. The [plan-model design](plan-model.md) describes the future shared next action and protection scoring; the simulation now shares first controls and uses the agreed cost/protection score; forecast/scenario changes and command-response verification remain separate work.

## Tests and change routing

Tests are grouped into `core/`, `forecasting/`, `storage/`, `dashboard/`, `browser/`, `planning/` and cross-layer `regressions/`. Run `python -m pytest -q` for all groups, including the legacy-named migration regression.

For a metric change, follow [adding a metric](../implementation/adding-a-metric.md). For state/data contracts and call hierarchies, use the [implementation index](../implementation/index.md). Styling and freshness changes follow [styling](styling.md); unresolved policies belong in [review findings](../reviews/findings.md).
