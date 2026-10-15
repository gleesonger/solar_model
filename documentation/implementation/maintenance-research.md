# Maintenance jobs, evaluation, and research data pipelines

These tools sit outside the managed collection/UI loop. Some mutate history; others only read SQLite and produce reports/artifacts. The [operations guide](../operations/runbook.md) contains invocation examples. This guide explains their function hierarchy and intermediate structures.

## 1. Tariff recalculation

Source: [recalculate_economics.py](../../recalculate_economics.py), [dashboard/control.py](../../dashboard/control.py).

```text
ControlTab recalculate callback
  validate inclusive dates and supervisor availability
  run.io_bound(run_recalculation)
    recalculation_command() -> argv list
    subprocess.run(check=True, capture_output=True)
      recalculate_economics.main(argv)
        load_config(), open_database()
        interval_rows(config)
        register configured_localtime(epoch) SQLite callback
        BEGIN IMMEDIATE + temporary tariff_intervals table
        join raw samples to interval rows -> UPDATE rates/band/costs
        rebuild_hourly_rollup(start,end,zone,connection=same_transaction)
        commit raw update and hourly rebuild together
  request supervisor restart after success
```

`interval_rows()` converts typed tariff periods into a SQL-friendly schedule. For each weekday it combines all import/export time boundaries, evaluates matching rules at each segment start, and emits:

```python
IntervalRow(
    effective_from="2026-09-12", next_effective=None,
    weekday=2, start_time="08:00", end_time="17:00",
    import_rate=4047, import_tariff_band="day", export_rate=1716,
)
```

Rates here are already ×10,000 integers. Weekday is SQLite `%w` convention (Sunday=0), unlike Python weekday (Monday=0). `sql_values()` returns a copied field dictionary for `executemany`.

The temporary table is connection-local, not a persisted schedule history. `configured_localtime` uses YAML timezone to match sample epoch to effective date, weekday and clock-time interval. The update joins `sigenstor` by sample key, replaces selected rates/band, and calculates raw integer costs using fixed-point energy/rate arithmetic. Dates not covered by an effective period stay unchanged.

Inclusive date bounds become `[local start midnight, midnight after end)` UTC epochs. Without an end bound, updates/rebuilds cover all available later dates. Raw updates and rollup replacement share one connection and `BEGIN IMMEDIATE` transaction; rebuild failure rolls both back. Whole overlapping UTC hours are rebuilt from all their raw samples, preserving neighboring dates in non-hour-offset zones. Initial schema opening is a separate initialization transaction.

## 2. Chronological operational forecast rebuild

Source: [forecast/rebuild_operational_adjusted_forecasts.py](../../forecast/rebuild_operational_adjusted_forecasts.py).

```text
main()
  open_database(), infer date bounds
  return preview unless --apply
  load_operational_inputs() once
  clear selected target range's adjusted fields; commit
  group collection timestamps by local collection date
  for each replay local day:
    for each recorded collection timestamp that day:
      calculate_adjusted_forecast(frames, as_of)
      accumulate target watt fields by destination snapshot
    add_adjusted_energy_values() per destination snapshot
    save_adjusted_forecasts_bulk() once for that day's outputs
```

Bounds inference reads minimum forecast target and maximum hourly actual timestamp. Even preview calls `open_database()`, which can migrate/backfill; preview means no adjusted replacement, not a strictly read-only database open.

Input frames use the shapes in [forecasting.md](forecasting.md). Additional `snapshot_local_date` groups recorded vintages. Writer buffer shape is:

```python
updates_by_snapshot: dict[str, dict[str, dict[str, float]]]
# collection ISO -> target ISO -> panel_n_adj_* physical W/Wh fields
```

The initial clear uses target-time bounds on all selected vintage rows. Replay uses each collection timestamp as `as_of` and the calculator's default target day, then integrates cumulative/day Wh in memory. Missing watt updates contribute zero during integration. It writes existing rows, not a new replay-snapshot table.

Live refresh loops over all target days in a current provider collection; this replay covers its same local day only. Those behaviors are not fully equivalent for future-day vintages. Clearing is committed before per-day writes; interruption can leave cleared/partially rebuilt history. Raw provider columns are untouched.

## 3. Raw-versus-adjusted performance report

Source: [forecast/evaluate_forecasts.py](../../forecast/evaluate_forecasts.py).

```text
main()
  forecast_performance(database, timezone, optional target date)
    inspect available raw/adjusted watts columns
    query collection/target epochs plus panel watt fields
    query actual non-null PV sample powers
    normalise powers; aggregate actuals to UTC target hours
    inner join forecast vintage/target rows with actual hours
    retain snapshot <= target and completed target hours; optional local date filter
  performance_report()
    metric_row(All) + metric_row(each nonempty lead band)
  print; optionally write CSV
```

Paired frame contains `snapshot`, `target` (floored UTC hour), `raw_kw`, `adjusted_kw`, `actual_kw`, `lead_hours`, alongside original queried fields. Site powers sum panels with null values replaced by zero; a row with every adjusted panel missing is excluded. Actual samples and targets must precede the UTC hour floor of `as_of` (default now). CLI `--as-of` freezes this cutoff; naive values use UTC. Actual power is sample mean, not meter energy. A completed historical hour can still have sparse coverage; no minimum sample threshold is imposed. Freezing the cutoff does not prevent later edits to historical records/configuration.

Report columns are lead label, sample count, raw/adjusted MAE kW, RMSE kW, signed bias kW, and MAE improvement %. Bands are 0–1h, 1–3h, 3–6h, 6–24h, and 24h+. Each forecast vintage/target pair is an observation; the same actual hour may participate in several vintages. This is not one prediction per target day or a comparison restricted to the midnight source.

No model fitting or adjusted writes occur. Optional CSV stores the report, not the entire pair frame.

## 4. Separate tree correction and retrospective backfill

Source: [forecast/solar_adjustment.py](../../forecast/solar_adjustment.py). This is not imported as the production collector's correction model.

```text
training_frame(connection, as_of, timezone)
  read bounded raw forecast history before current local-day target cutoff
  join completed target-hour PV power
  read_actual_interval_energy()
  rolling_energy_features() at each historical snapshot
  calendar/lead features + target_factor
fit_model() -> AdjustmentModel(model, features) or None
adjusted_values(raw_rows, model, energy_series, snapshot)
  inference factor × raw panel watts
  forward cumulative/day integration
backfill_adjusted_forecasts(database, as_of)
  fit one model at supplied/current as_of
  iterate oldest pending snapshots in batches of 24
  calculate features at each historical snapshot
  save adjusted target dictionaries
```

`read_actual_interval_energy()` sums available string `_energy_kwh_period` raw columns `/10000` and returns a UTC DatetimeIndex Series of interval kWh, excluding samples at/after cutoff. Rolling 1h/3h features require about 90% minute coverage plus observations near each boundary; otherwise each feature is `NaN`.

Training output columns:

```text
raw_solar_kw, forecast_lead_hours, hour_sin, hour_cos, day_sin, day_cos,
actual_solar_rolling_1h_kwh, actual_solar_rolling_3h_kwh,
target_factor, snapshot
```

`target_factor` is observed/raw site kW clipped to 0–3. The HistGradientBoosting model uses recency-weighted fitting with 21-day half-life, two-month snapshot window, and minimum 40 rows. `AdjustmentModel` holds the estimator and ordered feature tuple.

Inference accepts **ORM-decoded raw-row dictionaries**; it must not divide physical raw watts by storage scale again. No model means factor 1; non-positive raw means factor 1; predicted factors and resulting watts are clipped non-negative. Outputs are target-epoch dictionaries later converted to ISO-keyed writer dictionaries.

Backfill fits one model at its overall as-of time and applies it to older snapshots. Historical rolling features remain cutoff-filtered, but the model itself is retrospective. It is not the chronological operational replay and can mix tree-model output into pending operational history.

## 5. General load/solar model training

Source: [forecast/train_models.py](../../forecast/train_models.py).

- `read_actuals()` projects collection epoch, load power, PV power from `sigenstor`, requires both non-null, divides by 10,000, converts to configured-local index, and resamples hourly means into `load_kw/pv_kw`.
- `calendar_features()` creates local-hour and cyclic hour/day fields; optional weekday cyclic/weekend features distinguish load patterns.
- `load_features()` adds 1/2/24/48/168-hour load lags, optional 336h lag after sufficient history, and shifted rolling mean/std windows 1/3/6/24h. Target is `target_load_kw`; separate baseline is preceding 14 calendar days' same-hour mean when all 14 exist.
- `solar_features()` reads panels 1/2 raw power/day Wh, normalises to kW/kWh, selects latest eligible pre-local-midnight vintage per target, joins PV actuals, and adds lagged PV/residual plus calendar/lead fields. Target is a solar correction factor clipped 0–3.
- `split_days()` requires at least 12 feature days and returns chronological train/validation/test frames, about 70/15/15, preserving date boundaries.
- `evaluate()` excludes target/leaking/evaluation fields, selects numeric features, fits a tree estimator, and compares predictions with baseline on validation/test.

Artifacts are `<name>_model.pkl` containing `{model,features}` and `metrics.json` containing feature names and model/baseline scores (`rows`, `mae_kw`, `rmse_kw`, `energy_bias_kwh`). These artifacts are not loaded by the live application. Default database is `forecast/solar.db`, not the repository-root database.

## 6. Intraday recursive backtest

Source: [forecast/intraday_backtest.py](../../forecast/intraday_backtest.py). It imports `train_models` as a sibling module; invoke it by script path (`python forecast/intraday_backtest.py`), rather than assuming package-module execution works.

For test days, `fit()` trains using prior feature rows at midnight. `predict_load_day()` and `predict_solar_day()` expose actuals strictly before each chosen cutoff and recursively feed predicted values into later lag/rolling histories. Results are 24-element lists; positions before cutoff are `None`.

Solar `forecast_source()` retains `(snapshot,target,raw site kW,raw day kWh)` for panels 1/2. `raw_at()` chooses latest vintage ≤ cutoff for each target. Missing raw solar targets are assigned zero in this experiment. Predicted factor is clipped non-negative and multiplied by raw power. Workbook blocks compare hourly actuals with forecasts issued at different cutoffs, separated by day. This is an offline research model, not production scalar/residual logic.

## 7. Hierarchical load-profile report

Source: [forecast/evaluate_load_profile.py](../../forecast/evaluate_load_profile.py), currently a local untracked experiment.

`load_hourly_load()` queries hourly load `_period` sums grouped by UTC hour, divides by storage scale, and adds local `day`, `hour`, and weekday/weekend `day_type`. `predict_day()` uses only prior history in a 42-day window, with 14-day half-life; it blends type-specific hourly mean with an all-days hourly mean using `type_weight/(type_weight+4)`.

Predicted frame: `hour`, `prediction_kwh`, `type_weight` (24 rows). `evaluate()` joins observed target-day hours and appends day/type. Report adds absolute/signed kWh errors, prints overall/type MAE/bias, and daily total comparisons. It does not write the DB or replace the planner's exact-weekday profile.

## 8. Independent tariff simulation

Sources: [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py), [model_tariffs.py](../../check-tariffs/model_tariffs.py), [hardware.json](../../check-tariffs/hardware.json), [energy_plan.json](../../check-tariffs/energy_plan.json), [tarrifs.json](../../check-tariffs/tarrifs.json).

Both scripts perform work at module top level. Run intentionally from `check-tariffs` after reviewing hard-coded paths/output filenames; do not import them to inspect functions at runtime.

Extraction queries raw sample time, load/solar/grid period energies, tariffs, and battery availability. Fixed-point kWh becomes Wh by `/10`; rates become cents/kWh by `/100`. It writes:

```text
minute_data.csv:
end_time_stamp, excel_timestamp, period_covered_sec, load_wh, solar_pv_wh
```

It also writes an actuals workbook with hourly/daily/summary sheets. **Current extraction references legacy `inverter_battery_available_discharge_kwh`, whereas current schema uses `plant_battery_available_discharge_kwh`; it is not compatible with the migrated schema as written.** It also requires openpyxl and an external hard-coded database path.

Tariff model inputs are independent of Config: hardware W/Wh limits; tariff JSON enabled supplier/plan entries with cents/kWh rates and cents/day standing charge; named energy-plan date/time rules with solar enablement, grid-fill target, battery-export reserve, and solar-surplus target.

`run_model()` reads CSV string rows and converts them to numeric interval inputs, beginning each tariff run at 50% battery. It applies ordered route rules: solar serves load, charges battery; optional grid fill; battery serves remaining load unless force filling; optional battery export; remaining solar export; grid supplies remaining demand. Power W converts to Wh by interval seconds; the experiment's efficiency constant is 1.0.

Returned data is `(list of 24-field period rows, daily dict)`. Period fields cover timestamp/local/Excel times, supplier/tariff/energy-plan names, duration, load/solar Wh, battery start/end Wh, route Wh, grid import/export Wh, cents/kWh rates, and euro cost/revenue/standing/net costs. Daily keys are `(supplier,tariff_plan,date)` with Wh and cents accumulators. `aggregate_hourly()` sums flows/costs, keeps opening/closing battery, and averages rates; `write_workbook()` emits hourly/daily/summary frames in kWh/euros where appropriate.

This deterministic rule simulation is distinct from CVXPY optimisation, includes standing charges omitted from stored collector economics, and does not update production planning or database history.
