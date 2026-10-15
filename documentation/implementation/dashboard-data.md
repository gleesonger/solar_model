# Dashboard queries and intermediate data structures

Sources: [dashboard/data.py](../../dashboard/data.py), [dashboard/chart_data_sql.py](../../dashboard/chart_data_sql.py), [dashboard/models.py](../../dashboard/models.py), [dashboard/snapshot.py](../../dashboard/snapshot.py).

This guide covers database-to-dataset processing. The [presentation guide](dashboard-ui.md) follows these datasets into browser elements. Existing ORM/pandas compatibility helpers and the mounted Analysis SQL path are distinguished below.

## 1. Shared Recent data contract

```text
DashboardSnapshotStore.refresh()
  run.io_bound(load_recent_data, config, live_collector)
    load_day()
    load_latest_collection_local()
    live_collector.snapshot()
    load_recent_daily_energy(today, 7)
    load_recent_monthly_energy(today, 12)
  run.io_bound(load_device_information)
  DashboardSnapshot(version, loaded_at, recent_data, device_information)
```

`RecentData` is a **positional tuple**, not a dataclass:

| Position | Value | Shape |
| --- | --- | --- |
| 0 | `day` | 24-row current-day hourly DataFrame |
| 1 | latest persisted local collection | ISO string or `None`, used for freshness |
| 2 | live power | `LivePowerData`, copied from in-memory collector |
| 3 | recent daily summary | Descending date rows plus Total |
| 4 | monthly/lifetime summary | Twelve month rows plus Total and Lifetime |

`DashboardSnapshot` adds integer version, naive server `loaded_at`, and `device_information: list[dict[str,str]]`. `loaded_at` records successful refresh completion; it is not the last device reading or the freshness timestamp. The different loaders run separate queries; the snapshot publishes a complete group of results, but is not one atomic SQLite snapshot.

## 2. Current-day read hierarchy

```text
load_day(database_path, timezone, arrays, actuals_to_forecast)
  create_engine_for_database() + Session
  load_forecast_for_day()                  raw official vintage
  load_bod_adjusted_forecast_for_day()     first same-day adjusted vintage
  load_latest_adjusted_forecast_for_day()  newest same-day adjusted vintage
  load_actual_hourly(fill_missing=False)
    query SigenStorHourlySample
    actual_hourly_from_rollup_samples()
  load_actual_arrays_hourly(fill_missing=False)
    query SigenStorHourlySample
    actual_arrays_hourly_from_rollup_samples()
  aggregate_forecast() for each selected series
  merge all frames on hour
  blend latest solar forecast with known actuals
  allocate_solar_energy_to_arrays()
  fillna(0.0)
```

Actual queries select ORM hourly rows with `hour_start_utc >= local midnight` and `< next local midnight`, ordered by UTC hour. They are full ORM rows rather than projections of only displayed columns; ORM decorators return physical sums/values.

### Field mapping for hourly actuals

| Returned column | Stored source / processing |
| --- | --- |
| `hour` | Formatted bucket `00:00` … `23:00` |
| `solar`, `load` | Sum of `plant_pv_total_kwh_period`, `plant_load_total_kwh_period` |
| `grid_import`, `grid_export` | Sum of respective lifetime-meter `_period` deltas |
| `battery` | Summed charge `_period` minus summed discharge `_period` |
| `grid_import_cost`, `grid_export_revenue` | Sum stored cost/revenue periods |
| `net_cost`, `no_solar_battery_import_cost` | Sum stored comparator/net costs |
| `import_rate`, `export_rate` | Summed rollup rates / each rate's valid sample count |
| `import_cost_tariff__<band>` | Import cost grouped by band; columns depend on history |
| `actual_panel_n` | Initially summed string interval kWh from configured PV mappings |

Battery charge/discharge helper columns are removed after deriving net flow. Missing hour values remain `NaN` until the caller decides how to fill them. Multiple tariff rows in one UTC hour must contribute to the same display bucket.

### Forecast query selection

Each selector chooses a collection timestamp, then returns **all target rows for that collection**; `aggregate_forecast` later filters/labels the requested day.

| Selector | Collection query |
| --- | --- |
| `load_forecast_for_day` | Latest collection ≤ local midnight; no adjusted-field predicate |
| `load_bod_adjusted_forecast_for_day` | First same-day collection with a selected-day target holding adjusted cumulative Wh for any configured panel |
| `load_latest_adjusted_forecast_for_day` | Last same-day collection with a selected-day target holding adjusted watts for any configured panel |

Both adjusted selectors fall back to the official selector. Partial-panel snapshots qualify when at least one configured panel is populated; unconfigured panels and other target days do not qualify. This does not prove every panel/day is complete. The operational model's same-day cold-start raw fallback is not present in the official dashboard selector. Recent daily/lifetime collection dates use configured-timezone conversion of UTC epochs, independent of host timezone.

### Cumulative forecast rows to hourly energy

`aggregate_forecast(rows, day_start, arrays, source='adjusted'|'raw')` chooses a per-panel cumulative-Wh reader, builds points `(timestamp,panel_id,watt_hours)`, and then:

1. Sort by panel and timestamp; group by panel and target local date.
2. Difference cumulative Wh; use the first cumulative value as its first difference.
3. Clip negative differences to zero; divide by 1000 for kWh.
4. Label energy at the forecast timestamp's hour; pivot/sum panel values by hour.
5. Merge onto 24 clock-hour labels and add `forecast_total`.

Returned fields are `hour`, `forecast_panel_n` for configured arrays, and `forecast_total`. This function accepts ORM physical Wh, not fixed-point storage integers.

`load_day()` keeps panel columns from the newest adjusted view, renames the BoD total to `forecast_bod_total`, and raw total to `forecast_raw_total`. It adds `latest_forecast_total`: completed hours use actuals, the open hour uses partial actuals when present, future hours use adjusted forecasts. Completed missing actuals eventually become zero in final filling; they do not preserve an old forecast.

`allocate_solar_energy_to_arrays()` rescales `actual_panel_n` weights to total plant `solar`, using equal allocation when total usable weight is zero. This is why a returned per-array actual is an allocation, not simply the persisted string integral.

## 3. Daily and monthly summary queries

`load_daily_energy_totals(start_date,end_date,arrays)` reads bounded hourly rows and sums nine summary fields by configured-local date:

```text
date, solar_actual, solar_forecast, load, grid_import, grid_export,
grid_import_cost, grid_export_revenue, net_cost, no_solar_battery_import_cost
```

Actual fields map to the same energy/economics storage fields above. Forecast selection differs from the current-day table: it finds the **first collection on each date** using the forecast model's local-time hybrid expression, reads those collections, and aggregates adjusted energy. That hybrid uses host timezone. Today's summary forecast is scaled to elapsed hours/current-hour fraction, so it compares production-to-date rather than full-day production.

Daily wrappers add `period`, sort newest first, and append Total. Monthly wrappers group daily values into `YYYY-MM` rows, use labels like `Sep 2026`, fill absent months with missing values, append Total, then load lifetime daily history for a separate Lifetime row. `load_lifetime_start_date()` also uses local-time hybrids; this is a broader historical scan than twelve displayed months suggest.

`load_device_information()` reads device history descending ID, keeps the newest row per human `variable`, sorts labels case-insensitively, and returns `{variable,value,unit}` strings. It is not a historical as-of selector.

## 4. Analysis request and bounds contracts

`AnalysisRangeBounds` is a frozen dataclass with local `start_date/end_date` plus aware `start_at/end_at`. The shared controls use inclusive end-of-day `time.max`; an hour-count range anchors to the final hour of the selected date, not the current wall-clock hour. Export date bounds select whole local days.

Integer-second source queries use `ceil(start)` / `floor(end)` to retain final-second samples while excluding next midnight. Hour-range navigation moves the date anchor one calendar day in either direction; tooltips make this explicit. Other periods move by the selected count. The future-date cap still applies.

```python
ChartRequest(
    name="energy",
    selections=(("solar", None), ("forecast_total", None)),
    group_by="day", aggregation="sum",
    sample_resolution="hour", interval_minutes=60,
)
```

`selections` is ordered `(metric, optional_split_dimension)` pairs. The same metric may appear with different splits. Request names must be unique/nonempty. Group/split choices are hour/day/week/month/season/year; grouping and split may not be identical. Unknown non-tariff metrics are filtered out; invalid dimensions raise.

`load_analysis_range()` submits **two requests in one batch**:

- `energy`: unsplit energy/cost/rate/battery-flow, per-array actuals/forecasts, raw/adjusted totals, wildcard tariff cost.
- `telemetry`: power metrics plus `battery_level` and `soc_percent`, forced average. Single-date ranges use minute samples/one-minute buckets; multi-date ranges use hourly rollups/60-minute buckets.

## 5. SQL hierarchy and what each layer reads

```text
data.load_chart_data_batch()
  chart_data_sql.aggregate_chart_data_batch()
    _build_plan() per nonempty request
    _compose_batch(plans)
    register timezone callbacks / compensated aggregates
    execute one unified SQL statement
    group result rows by dataset name
    _format_dataset() per plan
```

`_QueryPlan` contains the `ChartRequest`, CTE definitions, output query, bound parameters, Python SQLite callbacks, resolved selections, and packed-output indices. It is an execution plan, not chart data.

| SQL component | Data source / transformation |
| --- | --- |
| Shared hourly source | Selected `sigenstor_hourly` columns within whole local dates |
| Shared minute source | Selected `sigenstor` columns inside inclusive timestamp bounds, only when needed |
| Collections/first collections | Distinct collection epochs inside selected bounds; minimum by configured local collection date |
| Shared forecast source | Selected fields for those first-per-day collections |
| Hourly actuals | Period fields `/10000` and sum; rates divide by their own valid sample counts |
| Telemetry daily | Raw/minute or rollup sums `/10000`, divided by count into interval means |
| Array weights | Mapped string energies; allocate plant solar with equal fallback |
| Forecast differences | `LAG` cumulative Wh by collection day/variant/panel/target date, clip decreases, `/1000` |
| Custom battery | Minute available-energy mean; if no minute records exist anywhere in range, charge-minus-discharge fallback |
| Tariff values | Band-specific summed import costs, expanding wildcard selections |
| Grid/values-by-hour | Fill calendar bucket scaffolding, join datasets, apply range filters |
| Final aggregation | Sum or mean by requested group and optional split |

Grid power signs are split per observation: minute queries use positive/negative magnitudes directly; hourly queries use stored split sums and `grid_power_sample_count`. Both yield means 1/1 kW for +2/-2 kW. Other nullable hourly telemetry/rates use their own `<field>_sample_count`. Missing values are excluded, zero is counted, and all-null stays missing.

SQL averages are mandatory for battery levels/rates/telemetry even when energy aggregation is `sum`. Telemetry computes per-day interval means before aggregating across dates, so it is not necessarily the mean of every raw sample across the whole selection. Hour-grouped multi-day data is a clock-hour profile rather than chronological timestamps.

`_compose_batch()` projects only referenced table columns, materialises shared bounded sources, interns common CTEs, namespaces request-specific parameters/functions, and combines outputs with `UNION ALL`. The single statement gives the batch one database read view; this guarantee applies to batched Analysis queries, not the broader Recent snapshot.

Date callbacks use configured timezone. Actual buckets use local clock hours; forecast callback uses elapsed UTC hours from local midnight. The implementation retains 24 hour slots, so those conventions need care at DST transitions.

## 6. SQL result to chart dictionary to Analysis frames

Intermediate SQL rows contain `dataset, bucket, selection, metric, split_order, split, value, packed`. Unsplit wide requests can use a JSON `packed` array; split/tariff rows use scalar values. Bucket-marker rows preserve categories even without data.

`_format_dataset()` assembles ordered periods and aligned series:

```python
{
    "periods": ["00:00", "01:00"],
    "series": [
        {"metric": "solar", "split": None, "data": [0.0, 0.0]},
        {"metric": "load", "split": "Mon", "data": [0.4, None]},
    ],
}
```

Missing data may remain `None` for means/series gaps; sum paths can coalesce to zero. Empty selection returns empty periods/series; an empty database range can still yield calendar-scaffold categories.

`chart_data_to_dataframe()` requires unsplit series, creates `float64` columns, and preserves full query precision. `load_analysis_range()` returns:

| `AnalysisRangeData` member | Columns / units |
| --- | --- |
| `energy` | `period`, solar/load/import/export kWh, `battery` net flow, per-array kWh, forecast kWh, costs, rates, dynamic tariff-cost columns |
| `power` | `time`, `solar`, `actual_panel_n`, `load`, `battery`, `inverter`, `grid_import`, `grid_export`; kW |
| `battery` | `time`, `available_energy_kwh`, `soc_percent` |

Energy `battery_flow` is renamed `battery`; telemetry `power_*` prefixes are removed. Custom previews use the same query engine but keep dictionaries and round values through `chart_value()` to one decimal before presentation.

## 7. Export and compatibility paths

Exports use dedicated [export_data_sql.py](../../dashboard/export_data_sql.py) aggregation rather than loading full ORM objects or using the chart-specific SQL. The public `analysis_export_dataframe()` adapter delegates to `load_analysis_export_dataframe()`:

```text
prepare_download()
  analysis_export_dataframe()
    export_data_sql.load_analysis_export_dataframe()
      build a small timezone-offset lookup for the requested date range
      group bounded raw electrical rows in native SQLite
      optionally select first-per-day forecast vintages and aggregate targets
      join periods and label columns within the same SQL statement
      pandas.read_sql_query() loads the final result; restore period timezones
  pandas CSV serialisation or one DataFrame-to-XlsxTurbo call (Rust writer)
```

The electrical query uses indexed collection-time bounds on `sigenstor` and aggregates all declared export fields directly into the requested local intervals. It deliberately uses raw samples, preserving per-field null handling, cumulative last readings, and sample-weighted means rather than substituting hourly-rollup semantics.

With forecasts enabled, SQL first selects distinct bounded collection epochs, finds the earliest collection per configured-local collection date, and joins only those vintages. Target bounds are applied before aggregation. Disabling forecasts omits all forecast-table references from the single export query. Calendar bucketing supports minute/hour/day/week/month/season/year, including configured timezone and UTC-offset transitions.

Native `SUM`/`AVG` handle numeric fields. For each last-reading field, `MAX(CASE WHEN field IS NOT NULL THEN reading_order END)` identifies its own final reading and a join retrieves that value; forecast reading order sorts collection then target time. Tariff labels use first-reading positions and ordered window `GROUP_CONCAT`. SQL also joins actual/forecast periods and assigns output labels. No Python SQLite functions/aggregates or pandas grouping/merging run on this path. A small Python-generated UTC-offset lookup supports native local-time bucketing; only final period values need timezone conversion. SQL removes fixed-point scaling once, and only aggregated result rows are converted into DataFrames. The resulting export preserves the previous field rules:

- `_kwh_period`, `_cost_period`, `_revenue_period`: sum.
- `_kwh` and `_watt_hours_day`: last reading.
- `import_tariff_band`: distinct non-null labels in reading order, joined with ` / `; all-missing labels remain missing.
- Other numeric fields, including cumulative forecast `_watt_hours`: average.
- Output column names are readable labels, with `Forecast ` prefixes. This exports stored-field aggregates, not chart forecast differences.

The retained `load_analysis_source_data()`/`AnalysisSourceData` and pandas export helpers serve older/reference paths and regression comparisons; current downloads do not materialise those source-row lists. Logs report the native SQL/read duration, data preparation, and file-generation time. See the [performance investigation](../reviews/download-performance.md) for measured results and limits.

Older `load_hourly_range`, `load_telemetry`, historical ORM/pandas frame helpers, and `custom_chart_preview_from_frames` remain in `data.py`. Some are used by the unmounted Hourly component or test comparison paths. The mounted `AnalysisTab` uses `load_analysis_range` and shared SQL; do not assume a similarly named old helper is its active implementation.
