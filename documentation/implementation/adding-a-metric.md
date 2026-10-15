# Worked change: add inverter temperature

This is an implementation recipe, **not an existing feature**. Example contract: `inverter_temperature_c`, physical °C, instantaneous level, nullable, averaged over valid readings. Obtain the address, device ID, signedness, word count, divisor and invalid-value rules from the actual device specification; this guide supplies no register address.

## 1. Decode and persist

1. In [sigen_register_map.json](../../sigen_register_map.json), add the verified register with `metric: inverter_temperature`, normalised `unit: C`, and `cumulative: false`. `column_name()` maps that pair to `inverter_temperature_c`. A temperature is not a cumulative counter and needs no `_period` field.
2. Add the nullable declaration to `SigenStorModbusSample` in [database.py](../../database.py):

   ```python
   inverter_temperature_c: Mapped[float | None] = mapped_column(Float)
   ```

   This file's `Float` aliases `ScaledInteger`: write physical 25.5, store integer 255000. Do not pre-scale decoded values. A register definition alone fails persistence because undeclared metrics are rejected.
3. Follow `read_registers → collect_once → save_modbus_sample → _add_hourly_sample`. Handle device sentinel values deliberately before persistence; do not treat a failed read or missing temperature as zero. Existing electrical read failures reject the whole interval.

## 2. Hourly statistics and existing databases

The model declaration automatically extends `HOURLY_MEASUREMENT_COLUMNS`, the mirrored hourly table, and `HOURLY_VALID_COUNT_COLUMNS`. Temperature stores a sum plus `inverter_temperature_c_sample_count`; readers divide by that valid count, not total samples. Do not add it to `CUMULATIVE_COLUMNS`.

`open_database()` adds missing columns but a current readiness marker does **not** trigger historical rebuilding for every new field. Existing history cannot acquire temperature that was never collected. Leave it missing. If raw temperature already exists but hourly statistics are absent, explicitly rebuild the relevant date range on a test copy before deployment. A global marker bump is appropriate only for a deliberate historical statistics migration, not routinely for every new metric.

For a new table, also register it in `open_database()`'s explicit table-creation sequence; a declaration alone is insufficient. Update the [schema inventory](../reference/schema.md).

## 3. Query and display

| Layer | Required change for the temperature example |
| --- | --- |
| [chart_data_sql.py](../../dashboard/chart_data_sql.py) | Add `temperature_inverter: inverter_temperature_c` to `TELEMETRY_COLUMNS`. This puts it on the valid-count mean path; do not route it through summed energy `ACTUAL_COLUMNS`. |
| [data.py](../../dashboard/data.py), `load_analysis_range` | Request the metric in the telemetry batch and extract a temperature frame. Its current fixed power/battery projections would otherwise discard it. |
| [models.py](../../dashboard/models.py), `AnalysisRangeData` | If adding a dedicated standard chart, add its `temperature` frame and update every constructor/caller, including empty/reference paths used by tests. Keep °C separate from the existing kW frame. |
| Standard Analysis chart | Add its `HistoricalCharts` field, construct/return it in `render_historical_charts`, then update it in `AnalysisTab._update_historical_charts`. Use a °C axis and `show_total=False`; follow [chart registration](dashboard-ui.md#registering-a-new-standard-analysis-chart). |
| Optional custom chart | Add the selectable metric/label in `AnalysisTab._show_custom_chart_builder` and the relevant metric list; add its unit to `charts.CUSTOM_CHART_UNITS`. Confirm it remains an average when the energy selector says Sum. |
| Full-data download | `SAMPLE_EXPORT_COLUMNS` derives from declared measurement fields. Confirm the new column is included and `export_aggregation()` selects average; avoid duplicating the schema list in the exporter. Chart CSV instead uses the component's extracted DataFrame. |
| Optional live display | Separately extend `LIVE_*_METRICS`, filtered register reads, `LivePowerData.values` and the chosen UI consumer in [live.py](../../dashboard/live.py)/[recent.py](../../dashboard/recent.py). Persisted collection does not automatically add a live value. |

## 4. Acceptance checks

Use temporary databases and fake register responses; see [isolated development](../operations/isolated-development.md).

- Decode known words to the specified physical temperature; verify negative values/sentinels according to the device contract.
- Save 25 °C, missing, 35 °C: hourly sum 60, valid count 2, mean 30 °C. Zero is a valid observation; all-missing stays missing.
- Compare incremental and rebuilt hourly results, including a tariff change within an hour. Raw/hourly writes must still roll back together on failure.
- Open an older temporary schema twice: preserve existing values, add the field/count, and leave unavailable historical temperature missing.
- Check one-day minute and multi-day hourly queries against expected means, then chart/table/CSV/full download units and values. No temperature Total row.
- In the browser, Apply a different range and confirm the new chart/table/export all change; check desktop/mobile and missing/stale states.

Update the owning overview/implementation guides and tests in the same change. The [metric semantics reference](../reference/metric-semantics.md) defines how this level differs from energy flows and counters.
