# Code map

For call hierarchies and structures crossing module boundaries, see the [detailed implementation guides](../implementation/index.md). This file is the module-level navigation map.

## Core files

| File | Responsibility / useful symbols |
| --- | --- |
| [docker_start.py](../../docker_start.py) | Child-process supervision; restart marker; shutdown |
| [main_data_collection.py](../../main_data_collection.py) | Collection scheduler and reconnect loop |
| [main_dashboard.py](../../main_dashboard.py) | NiceGUI page, startup/shutdown, snapshot subscriptions |
| [config.py](../../config.py) | Frozen configuration dataclasses; `load_config`, `validate_config` |
| [common.py](../../common.py) | Logging, timestamps, wall-clock scheduling, source-relative paths |
| [database.py](../../database.py) | ORM models, scaled storage, `SolarDatabase`, migrations, rollups |
| [modbus_collector.py](../../modbus_collector.py) | Register blocks, decoding, one-pass electrical/device collection |
| [forecast_collector.py](../../forecast_collector.py) | Provider requests and operational correction hook |
| [economics.py](../../economics.py) | Effective-date/rule lookup and interval costs |
| [datasheets.py](../../datasheets.py) | Inverter-model-to-nameplate-PV-power catalogue |
| [energy_plan_simulation.py](../../energy_plan_simulation.py) | Forecast intervals, device inputs, mixed-integer optimisation |
| [test_energy_plan_as_of.py](../../test_energy_plan_as_of.py) | Report CLI, as-of inputs, weighted load profile, scenarios, Excel output; despite its name, not a pytest test suite |
| [recalculate_economics.py](../../recalculate_economics.py) | Direct SQL tariff backdating and rollup rebuild |

## Dashboard package

- [layout.py](../../dashboard/layout.py): tab composition, shared CSS, browser-script injection.
- [models.py](../../dashboard/models.py): state/data/element dataclasses and shared panel colours.
- [snapshot.py](../../dashboard/snapshot.py): shared Recent/System Info cache and subscriptions.
- [live.py](../../dashboard/live.py): browser-activated live polling, daily meters, PV proportions.
- [recent.py](../../dashboard/recent.py): Live/Today, daily/monthly summaries, in-place updates.
- [anlaysis.py](../../dashboard/anlaysis.py): Analysis controls, chart builder, zoom and download actions. **Keep the existing misspelling when locating/importing this file.**
- [data.py](../../dashboard/data.py): summaries, forecast selection, exports, calendar helpers, chart adapters; also retains older ORM/pandas paths.
- [chart_data_sql.py](../../dashboard/chart_data_sql.py): selection-aware `ChartRequest` aggregation and batched SQL.
- [summary_data_sql.py](../../dashboard/summary_data_sql.py): shared native SQL daily/monthly/lifetime summaries, forecast differences and elapsed-day weighting.
- [export_data_sql.py](../../dashboard/export_data_sql.py): raw-sample/selected-vintage SQL aggregation for downloads; chronological last readings and tariff labels.
- [charts.py](../../dashboard/charts.py), [chart_display.py](../../dashboard/chart_display.py): ECharts options and reusable chart/table/export UI.
- [warnings.py](../../dashboard/warnings.py), [warnings.js](../../dashboard/assets/warnings.js), [live_updates.js](../../dashboard/assets/live_updates.js): notifications, persistent freshness banner, update indicator.
- [control.py](../../dashboard/control.py): operator recalculation/restart actions; [download_data_dialog.py](../../dashboard/download_data_dialog.py): interval CSV/Excel downloads.
- [system_info.py](../../dashboard/system_info.py), [tables.py](../../dashboard/tables.py): metadata table and total-row styling.
- [hourly.py](../../dashboard/hourly.py): older Hourly tab component, currently absent from `DashboardLayout`.
- [solar_power.svg](../../dashboard/assets/solar_power.svg): existing vector asset.

## Forecast and experimental tools

- [operational_model.py](../../forecast/operational_model.py): production solar scalar/residual correction and fixed daily load profiles.
- [rebuild_operational_adjusted_forecasts.py](../../forecast/rebuild_operational_adjusted_forecasts.py): chronological replacement of stored adjusted values.
- [evaluate_forecasts.py](../../forecast/evaluate_forecasts.py): raw/adjusted performance report.
- [evaluate_load_profile.py](../../forecast/evaluate_load_profile.py): report-only backtest of the shared production weekday/weekend load model.
- [check-tariffs/](../../check-tariffs): independent minute-data extraction and tariff simulations using `hardware.json`, `energy_plan.json`, and the intentionally named `tarrifs.json`.

## Change routing

- New metric: follow the [worked recipe](../implementation/adding-a-metric.md) through register JSON, declared storage, historical-data policy, valid counts, SQL, frame/chart registration, labels and tests.
- Forecast change: production model, collector integration, rebuild behavior, dashboard snapshot meaning, evaluation.
- Tariff change: configuration and matching, stored economics, recalculation, rollups, money charts.
- Styling change: follow [styling.md](styling.md); freshness policy lives in warning code, not tab layout.
