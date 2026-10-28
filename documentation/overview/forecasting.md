# Forecasting

For database queries, intermediate DataFrame fields, calibration arrays, update dictionaries, and W-to-Wh integration, see [forecasting implementation](../implementation/forecasting.md). Offline model pipelines are in [maintenance and research](../implementation/maintenance-research.md).

## Production model

The live hook is [solar_model.services.forecasting.refresh_adjusted_forecast](../../solar_model/planning/forecasts/solar.py), called by [solar_model/services/forecasting.py](../../solar_model/services/forecasting.py).

- Raw provider W, cumulative Wh, and daily Wh are preserved separately from adjusted output.
- For each local target day, correction uses the latest provider snapshot at/before midnight. A new installation without one uses its first same-day snapshot.
- Scalar calibration uses previous target days: 24 UTC-hour factors, 56-day lookback, 21-day recency half-life, weighted least squares through zero. Missing training yields scalar 1.
- Factors interpolate around hourly centres. Historical actuals are mean plant PV kW per UTC hour.
- Missing/non-finite observations do not contribute residuals; if none are usable, correction uses a neutral residual and retains the calibrated forecast.
- Completed current-day hours supply standardised residuals against hourly historical distributions. Residual half-life defaults to 1 hour; `forecast.solar.model_calibration_parameters.intraday.prior_effective_observations` uses decimal influence after one effective observation; the code default is one-third (strength 2), and the shipped YAML uses `0.3333` (strength approximately 2).
- Only targets at/after the current UTC hour floor are updated. The open hour uses earlier completed observations; its actual is excluded from residual inference.
- Future target days receive calibrated forecasts without today's residual.
- Site correction is allocated to panels by raw-power share and saved into the newest available **collection snapshot**, keeping each collection's output.
- Adjusted Wh integrates watts forward to the next timestamp, capped at 6 hours; the final point assumes 1 hour. Totals reset by local day.
- Model tuning lives in `forecast.solar.model_calibration_parameters` (day_ahead/intraday) and `forecast.load.model_calibration_parameters` (house load) in YAML and is loaded at startup. Current scalar/residual output is not clipped non-negative; see findings.

## Daily house-load model

The house-load model lives in [house_load.py](../../solar_model/planning/forecasts/house_load.py). Daily collection, the planning report and the evaluation CLI use its shared `predict_day()` helper. Inputs are summed `house_load_kwh_period` from hourly history before local target-day midnight, excluding conversion losses that the optimiser models separately. Recency-weighted weekday/weekend hourly means shrink toward all-days hourly means; there is no intraday adjustment. House-load tuning lives under `forecast.load.model_calibration_parameters`.

`forecast_house_load` stores generation time and each UTC target hour's household kWh. The entire day is inserted atomically and remains fixed across later runs/restarts/configuration changes. Failed writes can retry. Targets follow 23/24/25-hour local days, preserving repeated hours. Missing history stays null. Version 0.3.0 drops the undeployed `forecast_load` table and creates `forecast_house_load`; old local counter-based predictions are discarded, not relabelled. No historical house forecasts are fabricated.

Only Analysis Energy by hour includes **House Load (Forecast)**, alongside actual **House Load**, initially deselected. Recent Live/Today uses the full saved daily profile in House Load's BoD Forecast cell; its Latest/Raw Forecast cells are blank. The two Past tables, other chart types, non-hourly Energy groupings and the custom-variable picker omit house forecasts. The backtest CLI uses the same house-load helper without writing profiles.

## Dashboard forecast meanings

Source: [solar_model/storage/queries/dashboard.py](../../solar_model/storage/queries/dashboard.py), `load_day` and forecast selectors.

| View | Selection / meaning |
| --- | --- |
| Raw Forecast | Latest raw snapshot available at/before local midnight |
| BoD Forecast | First same-day snapshot containing selected-day adjusted cumulative Wh for any configured panel; fallback to pre-midnight snapshot |
| Latest Forecast | Newest same-day snapshot with selected-day adjusted watts for any configured panel, falling back to the pre-midnight snapshot; blended with actuals for completed hours and an observed partial current hour |
| Analysis/custom-chart forecasts | First collection within each selected local day/range, from shared chart SQL |

- These selection policies differ; do not treat every displayed forecast as the same vintage.
- Dashboard converts cumulative Wh differences to interval kWh and labels them at the forecast timestamp's starting hour.
- Current charts use 24 local clock-hour slots; DST repeated/skipped hours need care.
- Selecting adjusted output does not universally fall back to raw. The as-of planning script has its own fallback.

## Maintenance and research

- [rebuild_forecasts.py](../../solar_model/tools/rebuild_forecasts.py): `--apply` clears adjusted fields in a target-date range and replays collection times, committing by local day. It currently rebuilds same-day targets rather than every future day handled live.
- [evaluate_forecasts.py](../../solar_model/tools/evaluate_forecasts.py): MAE/RMSE/bias and improvement, overall and by lead band; compares stored vintages with hourly actuals and excludes collections after target time and hours not completed by `--as-of` (default now). Sparse completed hours remain eligible.
- [evaluate_load.py](../../solar_model/tools/evaluate_load.py): report-only evaluation of the production 42-day/14-day-half-life weekday/weekend profile shrunk toward all-day hourly means. Planning now uses this same prediction helper.

The three model blocks use the same names: `lookback`, `halflife`, and `prior_effective_observations`. The last field is the decimal fraction of influence after one unit of evidence, despite its retained name. Defaults are:

| `forecast.solar.model_calibration_parameters` (day_ahead/intraday) and `forecast.load.model_calibration_parameters` (house load) block | `lookback` | `halflife` | `prior_effective_observations` |
| --- | --- | --- | --- |
| `solar_day_ahead` | 56 days | 21 days | `null` (no prior in this model) |
| `solar_intraday` | `null` (earlier completed hours today) | 1 hour | one-third (YAML: `0.3333`) |
| `load` | 42 days | 14 days | `0.2` (20%) |

A numeric intraday `lookback` imposes an additional limit in hours, within today's completed observations. Day-ahead/load time settings use days. Code derives `prior_strength = 1 / prior_effective_observations - 1`, giving strength 4 for load and approximately 2 for the shipped intraday fraction. Fractions must be finite, greater than 0 and at most 1, with no rounding of derived strengths. The code default of exact one-third retains intraday strength 2 when model settings are omitted. Load evidence is the sum of recency weights; solar intraday evidence is effective sample size (`sum(weights)^2 / sum(weights^2)`). Missing model blocks retain the original defaults for existing YAML files. The collector, load-profile CLI and operational rebuild all consume the model settings; restart managed processes after changing YAML. Planning uses the same house-load helper and `forecast.load.model_calibration_parameters` tuning block; the unused legacy `energy_plan` history fields have been removed.

Commands and write effects are in [operations.md](../operations/runbook.md).

The general boosted load/solar training script and its dependent intraday backtest were removed on 2026-10-23. The older boosted solar-only correction/backfill script was also removed. Load-profile evaluation remains in `evaluate_load_profile.py`; production load prediction and solar correction live in `operational_model.py`. Scikit-learn is no longer a runtime dependency.
