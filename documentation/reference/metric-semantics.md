# Metric meanings across storage and outputs

Use this alongside the [exact schema](schema.md). Physical units apply below; raw SQLite measurement values are ×10,000 integers, while ORM values are already decoded. Full-data downloads and chart CSV are different products: downloads aggregate stored fields; chart CSV serialises the selected chart DataFrame.

## Measurement families

| Quantity / source | Meaning and units | Stored hourly statistic | Chart/summary meaning | Full-data download |
| --- | --- | --- | --- | --- |
| Plant solar/load/grid lifetime `*_total_kwh` | Cumulative meter, kWh | Maximum observed meter | Energy charts use its `_period` deltas, not the cumulative level | Last non-null reading per output period |
| Meter `*_kwh_period` | Delta since previous successful sample, kWh | Sum | Sum of measured interval energy; first/reset delta is missing | Sum |
| `plant_grid_power_kw` | Signed kW: import positive, export negative | Signed sum plus separate import/export magnitude sums and valid count | Import/export means are non-negative magnitudes, split before cancellation | Average signed raw power; not separate sign-series chart values |
| Solar/load/inverter power | Instantaneous kW | Sum plus each field's valid count | Mean power, never a meaningful summed Total | Average valid raw readings |
| `plant_battery_power_kw` | Signed device kW, passed through | Sum plus valid count | Mean signed power; do not infer its sign from charge-minus-discharge energy | Average valid raw readings |
| Battery charge/discharge `_period` | Separate non-negative measured flows, kWh | Sum | Analysis energy `battery_flow` = plant charge − discharge; positive means net charge | Each stored flow sums separately |
| `plant_battery_available_discharge_kwh` | Available discharge-energy **level**, kWh | Sum plus valid count | Battery-level mean; custom `battery` uses minute levels, with a charge-minus-discharge fallback when the range has no minute records | Last non-null reading because the field ends `_kwh` |
| `plant_battery_soc_percent` | State-of-charge level, % | Sum plus valid count | Mean SOC; no summed Total | Average valid raw readings |
| PV-string `*_power_kw` / `*_energy_kwh_period` | V×A power / trapezoidal interval energy | Power sum/count; energy sum | Live string power and chart allocations are not independent plant energy meters | Power average; period energy sum |
| Import/export rates | Configured money per kWh at sample time | Sum plus each rate's valid count | Sample means; not an energy-weighted effective price | Average valid raw readings |
| Cost/revenue `*_period` | Interval money; net = import cost − export revenue | Sum | Sum, including negative net values when export revenue exceeds cost | Sum |
| `import_tariff_band` | Import-rule label, not a number | Part of hourly primary key; missing becomes `Unknown` | May split import costs into band series | Distinct non-null raw labels in reading order, joined with ` / ` |
| Forecast `panel_n_*_watts` | Instantaneous provider/model W | No electrical rollup | Operational calibration uses kW helpers; these are distinct from chart forecast energy | Average stored W |
| Forecast `panel_n_*_watt_hours` | Cumulative target-day Wh | No electrical rollup | Difference successive values, clip decreases, convert to kWh using the selected vintage | Average stored cumulative Wh; not chart interval energy |
| Forecast `panel_n_*_watt_hours_day` | Total forecast Wh for target date | No electrical rollup | Full-day forecast total where consumed | Last non-null reading |

Recent Live/Today may replace database-derived values with live daily meters. Its battery Today value uses inverter daily charge minus discharge; Analysis uses plant lifetime-meter deltas. The common `battery` label therefore requires its view/unit context. See [dashboard presentation](../implementation/dashboard-ui.md).

## Missing values and averaging

- Zero is observed; `NULL`/`None` is missing. Hourly telemetry means divide by that field's valid count; 4/NULL → 4, 4/0 → 2, all-null → missing.
- Multi-day Analysis telemetry computes per-day interval means before averaging across dates. It is not generally the sample-weighted mean of all raw rows; full-data downloads average raw samples.
- Some energy scaffolding/final display paths fill missing with zero. Means/series gaps can remain missing. A displayed zero does not prove the meter reported zero; consult the owning [query contract](../implementation/dashboard-data.md).
- Storage rounds to 0.0001 physical unit; tables/charts can round further. Validate unrounded arithmetic separately from formatted display values.

## Time attribution

- Raw collection time and forecast target time are separate UTC instants. Local calendar buckets use `config.timezone`; do not use host-local ORM hybrids for new date selection.
- Meter deltas span previous/current successful samples. The whole delta uses its ending sample's tariff; gaps are not prorated across tariff/day boundaries. Display bucket attribution is adapter-specific; retained end-labelled helpers subtract a microsecond, while active SQL uses its documented collection/hour buckets.
- Forecast chart labels treat targets as starts; as-of planning treats them as ends. Adjusted Wh integrates watts forward to the next target. These conventions are documented differences, not interchangeable interval adapters.
- Analysis clock-hour profiles have 24 slots and can merge repeated DST hours. Planning arithmetic uses elapsed UTC time; chronological download hour buckets retain UTC offsets until Excel strips timezone metadata.

## Policies still requiring direction

This table describes current output. Whether availability downloads should use a mean instead of last, whether adjusted generation must be clipped non-negative, and how to unify forecast intervals or split long meter gaps remain [open decisions](../reviews/findings.md). Do not silently change these semantics while consolidating code or adding a metric.
