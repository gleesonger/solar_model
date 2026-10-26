# Maintenance jobs, evaluation, and research data pipelines

These tools sit outside the managed collection/UI loop. Some mutate history; others only read SQLite and produce reports/artifacts. The [operations guide](../operations/runbook.md) contains invocation examples. This guide explains their function hierarchy and intermediate structures.

## House-load comparison investigation

To migrate an existing database and backfill the production house-load series, run `python -m solar_model.tools.migrate_database <database.db>`. The CLI requires an existing file and invokes the same migrations as normal startup. Verify on a copy first. Successful migration stores `database_version = 0.2.0`; repeating it skips the completed house-load backfill. See [storage](storage.md) for transaction and gap semantics.

A read-only analysis on 2026-10-26 compared stored load-counter energy with estimated house-load energy for the 30 complete Europe/Dublin calendar days from 2026-09-26 through 2026-10-25. Counter deltas totalled **774.870 kWh**; integrated house load totalled **702.264 kWh**, a difference of **72.606 kWh (9.37% of counter load)**. Accepted power intervals covered **99.9%** of the elapsed time.

The analysis read raw `plant_load_total_kwh_period` and `plant_load_power_kw`, dividing stored integers by 10,000. Counter deltas retained collection-date attribution. House energy used trapezoidal integration of consecutive non-null power readings, skipped intervals longer than 120 seconds, and split accepted intervals at local midnight with interpolated boundary power. Coverage used actual local-day durations, including the 25-hour DST day. Synthetic checks verified constant-power integration across that DST day, midnight endpoints and gap handling.

The difference is a diagnostic comparison, not a verified loss measurement: sampling, gaps and counter attribution can also contribute. SQLite is opened read-only without application initialization, migrations or backfills. Dashboard metrics and database contents are unchanged.

The [comparison script](../../solar_model/tools/compare_house_load.py) is retained. Run `python -m solar_model.tools.compare_house_load [solar.db]` from the repository root. Options include `--days` (default 30), `--end` (exclusive ISO date, default latest measurement's local date), `--timezone` (default `Europe/Dublin`) and `--max-gap-seconds` (default 120). Temporary verification fixtures were removed.

## 1. Tariff recalculation

Source: [solar_model/tools/recalculate_economics.py](../../solar_model/tools/recalculate_economics.py), [solar_model/dashboard/tabs/control.py](../../solar_model/dashboard/tabs/control.py).

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

Source: [solar_model/tools/rebuild_forecasts.py](../../solar_model/tools/rebuild_forecasts.py).

```text
main()
  load_config(--config), open_database(), infer date bounds
  return preview unless --apply
  load_operational_inputs() once
  clear selected target range's adjusted fields; commit
  group collection timestamps by local collection date
  for each replay local day:
    for each recorded collection timestamp that day:
      calculate_adjusted_forecast(frames, as_of, models=config.forecast.model_calibration_parameters)
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

Source: [solar_model/tools/evaluate_forecasts.py](../../solar_model/tools/evaluate_forecasts.py).

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

## 4. Hierarchical load-profile report

Source: [solar_model/tools/evaluate_load.py](../../solar_model/tools/evaluate_load.py), a report-only evaluator of the production load model in `solar_model/planning/forecasts/solar.py`.

The CLI loads `--config` (default `config.yaml`) and uses `forecast.model_calibration_parameters.load`; configured database/timezone are defaults, overridable by CLI flags. The shared production `load_hourly_load()` queries hourly load period sums grouped by UTC hour, divides by storage scale, and adds local `day`, `hour`, and weekday/weekend `day_type`. `predict_day()` uses only prior history within `settings.lookback` days (default 42), weighted by `settings.halflife` days (default 14). It blends type-specific and all-days hourly means with `type_weight/(type_weight+settings.prior_strength)`. The shared field `prior_effective_observations` is a decimal influence fraction after one unit of weighted evidence, default `0.2` (20%); its derived strength is `1 / prior_effective_observations - 1` (default 4). Without matching day-type observations the all-days hourly mean is used, including at fraction `1` (100% influence); hours with no observations remain missing. Pure helper calls may receive settings explicitly or use the original configuration defaults.

Predicted frame: `hour`, `prediction_kwh`, `type_weight` (24 rows). `evaluate()` joins observed target-day hours and appends day/type. Report adds absolute/signed kWh errors, prints overall/type MAE/bias, and daily total comparisons. It does not write the DB or replace the planner's exact-weekday profile.

## 5. Independent tariff simulation

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
