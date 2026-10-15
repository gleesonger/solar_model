# Forecasting

For database queries, intermediate DataFrame fields, calibration arrays, update dictionaries, and W-to-Wh integration, see [forecasting implementation](../implementation/forecasting.md). Offline model pipelines are in [maintenance and research](../implementation/maintenance-research.md).

## Production model

The live hook is [forecast.operational_model.refresh_adjusted_forecast](../../forecast/operational_model.py), called by [forecast_collector.py](../../forecast_collector.py).

- Raw provider W, cumulative Wh, and daily Wh are preserved separately from adjusted output.
- For each local target day, correction uses the latest provider snapshot at/before midnight. A new installation without one uses its first same-day snapshot.
- Scalar calibration uses previous target days: 24 UTC-hour factors, 56-day lookback, 21-day recency half-life, weighted least squares through zero. Missing training yields scalar 1.
- Factors interpolate around hourly centres. Historical actuals are mean plant PV kW per UTC hour.
- Completed current-day hours supply standardised residuals against hourly historical distributions. Residual half-life is 1 hour; effective sample strength is shrunk using a 2-hour constant.
- Only targets at/after the current UTC hour floor are updated. The open hour uses earlier completed observations; its actual is excluded from residual inference.
- Future target days receive calibrated forecasts without today's residual.
- Site correction is allocated to panels by raw-power share and saved into the newest available **collection snapshot**, keeping each collection's output.
- Adjusted Wh integrates watts forward to the next timestamp, capped at 6 hours; the final point assumes 1 hour. Totals reset by local day.
- Constants live in code, not YAML. Current scalar/residual output is not clipped non-negative; see findings.

## Dashboard forecast meanings

Source: [dashboard/data.py](../../dashboard/data.py), `load_day` and forecast selectors.

| View | Selection / meaning |
| --- | --- |
| Raw Forecast | Latest raw snapshot available at/before local midnight |
| BoD Forecast | First same-day snapshot containing panel-1 adjusted cumulative Wh; fallback to pre-midnight snapshot |
| Latest Forecast | Newest same-day snapshot with panel-1 adjusted watts, falling back to the pre-midnight snapshot; blended with actuals for completed hours and an observed partial current hour |
| Analysis/custom-chart forecasts | First collection within each selected local day/range, from shared chart SQL |

- These selection policies differ; do not treat every displayed forecast as the same vintage.
- Dashboard converts cumulative Wh differences to interval kWh and labels them at the forecast timestamp's starting hour.
- Current charts use 24 local clock-hour slots; DST repeated/skipped hours need care.
- Selecting adjusted output does not universally fall back to raw. The as-of planning script has its own fallback.

## Maintenance and research

- [rebuild_operational_adjusted_forecasts.py](../../forecast/rebuild_operational_adjusted_forecasts.py): `--apply` clears adjusted fields in a target-date range and replays collection times, committing by local day. It currently rebuilds same-day targets rather than every future day handled live.
- [evaluate_forecasts.py](../../forecast/evaluate_forecasts.py): MAE/RMSE/bias and improvement, overall and by lead band; compares stored vintages with hourly actuals and excludes collections after target time.
- [solar_adjustment.py](../../forecast/solar_adjustment.py): separate HistGradientBoosting correction with rolling solar-energy features. Its retrospective backfill fits one model at the supplied/current as-of time; it is not a leakage-safe chronological replay.
- [train_models.py](../../forecast/train_models.py): load/solar tree experiments, 70/15/15 chronological whole-day split, pickle models/JSON metrics; not loaded by production.
- [intraday_backtest.py](../../forecast/intraday_backtest.py): repeated intraday research predictions and Excel report.
- [evaluate_load_profile.py](../../forecast/evaluate_load_profile.py): report-only 42-day/14-day-half-life weekday/weekend profile shrunk toward all-day hourly means. It differs from planning's exact weekday/hour profile.

Commands and write effects are in [operations.md](../operations/runbook.md).
