# Operational forecast correction: frames, calculations, and writes

Source: [forecast/operational_model.py](../../forecast/operational_model.py). The [forecasting overview](../overview/forecasting.md) explains model intent and displayed forecast meanings. This guide traces the production implementation; tree-model tools are in [maintenance-research.md](maintenance-research.md).

## 1. Function hierarchy

```text
forecast_collector.collect_once()
  refresh_adjusted_forecast(database, collection_time, timezone)
    load_operational_inputs(database)
      _load_inputs(database_path)
    choose latest snapshot <= as_of
    for each local target day in that snapshot:
      calculate_adjusted_forecast(frames, as_of, target_day)
        _official_day_frame()
          _day_bounds()
          merge actuals on target_hour
        _training_frames()
          _official_day_frame() for preceding target days
        _fit_scalars()
        _hour_distributions()
        _interpolate() -> completed-hour baseline
        _today_residual()
        _prediction_fields() -> per-target adjusted watts
        map official-hour predictions onto current snapshot targets
    database.save_adjusted_forecasts()       watts pass
    _recalculate_energy()
      read the saved snapshot
      integrate by local target day
      database.save_adjusted_forecasts()     Wh pass
```

`calculate_adjusted_forecast()` computes values without writing. `refresh_adjusted_forecast()` owns live orchestration and writes. `add_adjusted_energy_values()` is a separate in-memory integration helper used by historical replay.

## 2. What is pulled from SQLite

`_load_inputs()` uses direct sqlite3/pandas queries:

```sql
SELECT * FROM forecast_solar;
SELECT collected_at_utc, plant_pv_power_kw
FROM sigenstor WHERE plant_pv_power_kw IS NOT NULL;
```

It loads the whole available forecast history and PV sample history, not just the latest day or hourly rollup. Original SQL numeric columns remain fixed-point storage values. Normalisation adds physical helper columns instead of dividing every original forecast field in place.

| Forecast-frame field | Type / units / derivation |
| --- | --- |
| Original `panel_n_raw_*`, `panel_n_adj_*` | Raw SQL scaled numeric values; null is generally pandas `NaN` |
| `snapshot` | UTC-aware pandas timestamp, replacing collection epoch column |
| `target` | UTC-aware pandas timestamp, replacing forecast epoch column |
| `target_hour` | `target.floor('h')`, UTC |
| `raw_1_kw` … `raw_4_kw` | Raw panel watts `/10000/1000`, with missing raw values replaced by zero |
| `raw_total_kw` | Sum of four physical helper columns |

Actual samples become a separate frame with `target_hour` and physical `plant_pv_power_kw`. The sample collection epoch is floored to a UTC hour, grouped by mean, then divided by 10,000. This is observed hourly **mean power**, not meter-derived hourly energy. It does not require a complete hour of samples.

```python
# Abbreviated normalised rows; original scaled columns still coexist
forecasts_row = {
    "snapshot": pd.Timestamp("2026-09-14T22:00:00Z"),
    "target": pd.Timestamp("2026-09-15T09:00:00Z"),
    "target_hour": pd.Timestamp("2026-09-15T09:00:00Z"),
    "raw_1_kw": 2.0, "raw_2_kw": 1.0,
    "raw_3_kw": 0.0, "raw_4_kw": 0.0, "raw_total_kw": 3.0,
}
actuals_row = {"target_hour": pd.Timestamp("2026-09-15T09:00:00Z"),
               "plant_pv_power_kw": 3.3}
```

Keep the two numeric representations distinct: model helpers are kW, while original SQL watts columns are still W × 10,000.

## 3. Official source versus destination vintage

`_day_bounds(local_day, zone)` returns UTC-aware start/end timestamps for local calendar midnights, using a calendar-day offset. `_official_day_frame()` selects the latest **collection timestamp** ≤ target-day start, then keeps that snapshot's target hours within the day. If no earlier collection exists, it chooses the earliest same-day collection. If nothing qualifies, it returns `None`.

The selected official frame is left-joined to actuals by `target_hour`, adding nullable physical observed power. Collection selection happens before target filtering; an older snapshot with targets is not searched if the newest selected snapshot lacks them.

The destination is separately chosen as the latest available collection ≤ `as_of`. Predictions derived from the official raw vintage are written against matching target hours in this current collection. Thus a stored current raw value and stored adjusted value may come from **different underlying raw vintages**: the current provider response versus the midnight-anchored source.

This distinction explains why correction is not simply “multiply the newest provider row by a scalar”. Returned type is:

```python
tuple[pd.Timestamp, dict[str, dict[str, float]]] | None
# first element: destination collection timestamp
# mapping key: destination target ISO timestamp
# inner mapping: adjusted-field names in physical units
```

## 4. Calibration structures

`_training_frames()` concatenates official frames for target days preceding the requested target day. Each row carries raw helper powers, UTC times, and joined observed plant power. `_fit_scalars()` selects complete `(raw_total_kw, plant_pv_power_kw, target)` rows within the last 56 days relative to `as_of`.

For each UTC hour 0–23:

```text
weight = exp(-ln(2) × max(age_days, 0) / 21)
scalar = sum(weight × raw_kw × actual_kw) / sum(weight × raw_kw²)
```

Output is `numpy.ndarray`, shape `(24,)`, dimensionless. Missing usable hour/denominator leaves scalar 1. `_interpolate()` evaluates this array at target hour/minute, with centres at `00:30 … 23:30` and wraparound at midnight.

`_hour_distributions(actuals, day_start)` reads actual hours before target-day start, grouping by UTC hour-of-day. With at least three observations and non-trivial spread, it returns:

```python
dict[int, tuple[float, float]]  # UTC hour -> weighted mean kW, sigma kW
```

It uses 21-day age weights but does not apply the scalar's explicit 56-day cutoff. The distribution mean is calculated; residual normalisation uses sigma.

## 5. Intraday inference and intermediate frame

`cutoff = as_of.floor('h')`. `past` contains official-day rows strictly before that cutoff, with `baseline_kw = interpolated_scalar × raw_total_kw`.

`_today_residual()` constructs score/weight arrays only for rows with finite observed power, baseline power and raw power, positive raw power, and a finite positive distribution sigma. Missing observations are excluded, never treated as zero. Non-finite scores/weights and zero weights are also excluded:

```text
score = (actual_kw - baseline_kw) / sigma_for_target_UTC_hour
weight = exp(-ln(2) × elapsed_hours / 1)
effective = sum(weights)² / sum(weights²)
strength = effective / (effective + 2)
```

It returns `(weighted residual score, strength, observation count)`, or three zeros if none qualify. Current/open-hour actuals are excluded by the strict hour cutoff. For a future local target day, residual and strength are explicitly zero.

The loaded actual frame itself contains all database history. Filters in these helpers, rather than the initial query, define which observations participate. If using the pure calculator for historical experiments, examine those filters for the requested `as_of` and `target_day`; do not infer an automatic database cutoff from its signature.

## 6. Prediction dictionary and persistence

`source_future` keeps official-frame target hours ≥ max(current UTC hour floor, local target-day start). For each row `_prediction_fields()` computes:

```text
correction_kw = sigma × strength × residual
panel_share = panel_raw_watts / site_raw_watts  (zero if site raw is zero)
panel_adj_watts = scalar × panel_raw_watts + correction_kw × 1000 × share
```

Intermediate dictionary keys are integer target epochs. It is then indexed by `target_hour` and mapped onto destination target timestamps. Multiple provider timestamps within one UTC hour share the hour-mapped fields; the dictionary construction retains the last source row encountered for that hour.

```python
# Final physical-unit writer contract, abbreviated to one panel
{
    "2026-09-15T09:00:00+00:00": {"panel_1_adj_watts": 2200.0, ...},
    "2026-09-15T10:00:00+00:00": {"panel_1_adj_watts": 2600.0, ...},
}
```

The writer updates existing rows; it does not create forecast targets. Negative residuals are not clipped non-negative by this implementation. `_prediction_fields()` supplies fields for all four panel IDs, including zero-share panels.

## 7. Saved watts to cumulative/day Wh

`_recalculate_energy()` rereads the entire current snapshot through raw SQL. It converts stored adjusted watts `/10000`, treats missing adjusted watts as zero for integration, and groups targets by configured local date.

- Sort each day's target times.
- For each point, use its watts through the next target; interval hours are clamped to 0–6.
- Last target assumes one hour.
- Accumulate `panel_n_adj_watt_hours` from local-day zero.
- Copy each panel's final total to `panel_n_adj_watt_hours_day` on every target row in that day.
- Save the resulting physical Wh dictionary in a second transaction.

Example: 2200 W followed one hour later by 2600 W yields cumulative Wh 2200 then 4800 if the second point is last; daily Wh is 4800 in both rows. These are forward rectangular intervals, not trapezoidal integration like PV-string actual energy.

Historical replay's `add_adjusted_energy_values()` consumes an in-memory update dictionary in physical W and must **not** divide those values by 10,000. Its structure and writer are described in [maintenance-research.md](maintenance-research.md).

## 8. Later retrieval

- Recent/raw/BoD/latest selection returns ORM physical W/Wh and differences cumulative Wh into chart energy; see [dashboard-data.md](dashboard-data.md).
- Performance evaluation reads raw storage, normalises W to kW, joins target-hour observations, and evaluates vintage/target pairs.
- Planning selects one collection at the as-of cutoff and converts cumulative Wh into its own end-labelled intervals; see [planning.md](planning.md).
- Raw values are never changed by adjusted writers. Raw collection itself can merge/update panel fields at the same composite key; “immutable raw” describes model ownership, not a database immutability constraint.
