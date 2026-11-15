# Household usage: 2025 provider versus 2026 House Load

Read-only comparison performed 2026-11-15. The request referred to 2015/2016; the supplied files contain 2025/2026, which are used here. Per the user, 2025 predates the PV/battery installation, so provider import represents household consumption.

## Period and totals

The common complete-calendar-day window is **23 August-14 November inclusive**, 84 days in each year. Partial DB days August 22 and November 15 are excluded. The CSV contains 2025 readings through year-end, so the 2026 provider cutoff on November 11 does not limit this comparison. Dates and clock times are matched in `Europe/Dublin`, rather than shifted to match weekdays.

| Basis | 2025 provider import | 2026 House Load | Interpretation |
| --- | ---: | ---: | --- |
| All available readings in the 84-day window | 1,919.199 kWh | 1,885.092 kWh | Different missing-data coverage; these are recorded sums, not gap-filled full consumption. |
| Fully covered paired half-hours | 1,905.281 kWh | 1,876.132 kWh | Same 2,000.5 hours in each year: -29.149 kWh (-1.530%). |
| Equivalent daily usage on paired intervals | 22.858 kWh/day | 22.508 kWh/day | Paired energy / paired hours * 24; an exposure-normalised figure, not an observed complete-day mean. |

Each full calendar window contains 2,017 elapsed hours, including its autumn clock change. Provider readings cover 2,016 hours (99.950%); valid DB House Load intervals cover 2,012.6625 hours (99.785%), leaving 4.3375 hours unmeasured. No missing energy was estimated or filled with zero.

The primary comparison retains 4,001 fully covered matched half-hours (99.182% of the 2026 calendar window). The repeated hour occurs on October 26 in 2025 and October 25 in 2026; the second occurrence has no counterpart on the same calendar date in the other year and is excluded in both years. Two additional 2025 provider intervals, starting October 31 at 23:30 and November 1 at 00:00, are missing. Twenty-nine 2026 half-hours have incomplete House Load coverage and are excluded.

## Matched usage by month

Only the fully measured paired intervals contribute to these totals. August is August 23-31; November is November 1-14.

| Period | Paired hours | 2025 kWh | 2026 kWh | Change |
| --- | ---: | ---: | ---: | ---: |
| Aug 23-31 | 211.0 | 127.843 | 176.832 | +38.32% |
| September | 715.5 | 588.323 | 614.350 | +4.42% |
| October | 739.0 | 811.180 | 653.555 | -19.43% |
| Nov 1-14 | 335.0 | 377.935 | 431.395 | +14.15% |

Usage is broadly similar overall, with substantial variation between months. October is down 19.43%, while the matched portions of August, September and November are higher. This comparison does not separate changes in weather, occupancy, appliances or schedules, and does not measure savings caused by the PV/battery system.

## Hourly usage profile

Values are **average kWh consumed per 60 minutes within each clock-hour band**, calculated as paired energy / paired elapsed hours for that band. Each row averages the same calendar dates and half-hours in both years; available paired exposure varies from 80.5 to 84 hours by band. These are hourly energy averages, numerically equal to mean kW, not the total kWh for the entire date range. The hourly CSV also includes period totals and exposure counts.

![Hourly household usage comparison](system-review-evidence/house-load-year-comparison-2026-11-15/hourly-usage.png)

| Hour starting | 2025 kWh/hour | 2026 kWh/hour | Change |
| --- | ---: | ---: | ---: |
| 00:00 | 0.471 | 0.539 | +14.6% |
| 01:00 | 0.502 | 0.594 | +18.3% |
| 02:00 | 0.496 | 1.331 | +168.5% |
| 03:00 | 0.408 | 1.211 | +196.7% |
| 04:00 | 0.562 | 0.986 | +75.4% |
| 05:00 | 0.829 | 0.715 | -13.7% |
| 06:00 | 1.322 | 0.549 | -58.5% |
| 07:00 | 1.485 | 0.652 | -56.1% |
| 08:00 | 1.761 | 0.905 | -48.6% |
| 09:00 | 1.531 | 1.042 | -32.0% |
| 10:00 | 1.405 | 1.007 | -28.3% |
| 11:00 | 1.414 | 1.027 | -27.4% |
| 12:00 | 1.224 | 1.106 | -9.7% |
| 13:00 | 0.995 | 0.907 | -8.8% |
| 14:00 | 1.033 | 0.770 | -25.5% |
| 15:00 | 0.895 | 0.708 | -20.9% |
| 16:00 | 0.811 | 0.743 | -8.4% |
| 17:00 | 1.068 | 0.857 | -19.8% |
| 18:00 | 1.126 | 0.750 | -33.4% |
| 19:00 | 1.079 | 0.798 | -26.0% |
| 20:00 | 0.805 | 1.321 | +64.1% |
| 21:00 | 0.616 | 1.885 | +206.0% |
| 22:00 | 0.571 | 1.346 | +135.8% |
| 23:00 | 0.439 | 0.744 | +69.5% |

The strongest profile changes are lower usage at 06:00-11:00 and higher usage at 02:00-04:00 and 20:00-23:00. The largest hourly average in 2025 is 08:00-09:00 (1.761 kWh/hour); in 2026 it is 21:00-22:00 (1.885 kWh/hour). Timing patterns alone do not identify which appliances or controls caused the change.

## Household usage valued at the latest configured tariff

Added on 2026-11-15 using the latest effective-dated period in repository-root `config.yaml`: **Yuno Energy - EV Variable Smart**, effective **2026-09-12**. The same latest import schedule is applied to both years throughout the whole comparison, including dates before its effective date. This is a common-price comparison, not reconstruction of historical bills.

| Local time, every day | Band | EUR/kWh |
| --- | --- | ---: |
| 00:00-02:00 | night | 0.2146 |
| 02:00-06:00 | EV | 0.0859 |
| 06:00-08:00 | night | 0.2146 |
| 08:00-17:00 | day | 0.4047 |
| 17:00-19:00 | peak | 0.4538 |
| 19:00-23:00 | day | 0.4047 |
| 23:00-24:00 | night | 0.2146 |

The same 4,001 fully covered paired half-hours give the following hypothetical import costs if all household demand were supplied from the grid at its time of consumption:

| Period | 2025 cost EUR | 2026 cost EUR | Change EUR | Change |
| --- | ---: | ---: | ---: | ---: |
| Aug 23-31 | 45.25 | 61.62 | +16.38 | +36.19% |
| September | 200.50 | 212.45 | +11.95 | +5.96% |
| October | 278.83 | 205.06 | -73.76 | -26.45% |
| Nov 1-14 | 128.39 | 125.69 | -2.70 | -2.10% |
| **Total** | **652.96** | **604.82** | **-48.14** | **-7.37%** |

The repriced 2026 demand costs **EUR 48.14 less (-7.37%)**, despite energy being only 1.53% lower. The larger cost reduction reflects the changed time-of-day mix. In November, demand rises 14.15% while its repriced cost falls 2.10%.

This values House Load at import prices, consistent with the model's No Solar Cost concept. It does not represent the actual 2026 grid bill or quantify PV/battery savings. Standing charges, export credits and other billing adjustments are excluded; rates are used exactly as configured without adding or removing VAT. No assumption about the configured rates' VAT treatment is made.

### Hourly comparison in euros

Average euros per 60 minutes within each clock-hour band, using the identical paired exposure as the energy comparison. Period totals by hour and half-hour calculations are also saved as CSVs.

![Hourly household cost comparison](system-review-evidence/house-load-year-comparison-2026-11-15/hourly-cost.png)

| Hour starting | EUR/kWh | 2025 EUR/hour | 2026 EUR/hour |
| --- | ---: | ---: | ---: |
| 00:00 | 0.2146 | 0.101 | 0.116 |
| 01:00 | 0.2146 | 0.108 | 0.127 |
| 02:00 | 0.0859 | 0.043 | 0.114 |
| 03:00 | 0.0859 | 0.035 | 0.104 |
| 04:00 | 0.0859 | 0.048 | 0.085 |
| 05:00 | 0.0859 | 0.071 | 0.061 |
| 06:00 | 0.2146 | 0.284 | 0.118 |
| 07:00 | 0.2146 | 0.319 | 0.140 |
| 08:00 | 0.4047 | 0.713 | 0.366 |
| 09:00 | 0.4047 | 0.620 | 0.421 |
| 10:00 | 0.4047 | 0.568 | 0.408 |
| 11:00 | 0.4047 | 0.572 | 0.416 |
| 12:00 | 0.4047 | 0.495 | 0.447 |
| 13:00 | 0.4047 | 0.403 | 0.367 |
| 14:00 | 0.4047 | 0.418 | 0.311 |
| 15:00 | 0.4047 | 0.362 | 0.286 |
| 16:00 | 0.4047 | 0.328 | 0.301 |
| 17:00 | 0.4538 | 0.484 | 0.389 |
| 18:00 | 0.4538 | 0.511 | 0.340 |
| 19:00 | 0.4047 | 0.436 | 0.323 |
| 20:00 | 0.4047 | 0.326 | 0.535 |
| 21:00 | 0.4047 | 0.249 | 0.763 |
| 22:00 | 0.4047 | 0.231 | 0.545 |
| 23:00 | 0.2146 | 0.094 | 0.160 |

Calculation: multiply each unrounded paired half-hour kWh value by the latest band rate at that interval's local start, then sum before rounding monetary totals. All tariff boundaries align to half-hours. The application's `TariffRule` and `tariff_rule` matcher select the band; the historical-date period selector is intentionally bypassed so both years use the latest period. Monthly, hourly and tariff-band costs reconcile to the same overall totals.

Additional local evidence: `price_usage.py`, `cost-summary.json`, `half-hour-cost-comparison.csv`, `hourly-cost-comparison.csv`, `monthly-cost-comparison.csv`, `tariff-band-cost-comparison.csv`, `render_cost_chart.py`, and `hourly-cost.png` in the same evidence directory. Reproduce with `python documentation/reviews/system-review-evidence/house-load-year-comparison-2026-11-15/price_usage.py`; this reads the existing comparison CSV and configuration, without opening the database.

Configuration SHA-256 for this pricing: `43dd789c404b58e761d03e2ed2e130bc694b00ddfc57d75bd0ba1bd44eb39cc7`.

## Measurement and verification

- The 2025 source is the supplied `HDF_calckWh_10306341332_15-11-2026.csv`; use its explicitly labelled import kWh directly. End-labelled half-hours are converted to start times before matching, preserving both DST folds. The source hash is recorded in the [grid-meter comparison](provider-meter-comparison-2026-11-15.md).
- The 2026 source is stored `house_load_kwh_period`, divided by 10,000, rather than grid import or device Load counters. As documented in [storage](../implementation/storage.md), this field integrates household power and omits intervals over 120 seconds. It includes demand met by solar and battery as well as demand met by the grid.
- Each stored House Load interval is distributed by elapsed seconds over overlapping half-hours. Matching uses month, day, local start time and fold. Partial-coverage half-hours are excluded from both years in the primary comparison. Stored period energy is retained, including its 0.0001 kWh rounding; no fresh power integration or gap imputation is substituted.
- The summed 2026 calendar-window allocation equals the direct stored end-attributed sum, 1,885.0915 kWh, to floating-point tolerance. Monthly, hourly and daily paired totals reconcile to the same primary totals.
- SQLite was opened with `mode=ro` and `query_only`; no application opener, migrations, data changes, model simulations or application test suite were used. The DB snapshot contains 121,535 rows and ends 2026-11-15 09:40 UTC.

Local evidence (ignored by Git) is in `documentation/reviews/system-review-evidence/house-load-year-comparison-2026-11-15/`: `compare.py`, `summary.json`, `half-hour-comparison.csv`, `hourly-comparison.csv`, `monthly-comparison.csv`, `daily-comparison.csv` and `hourly-usage.png`. Run the analysis from the repository root with `python documentation/reviews/system-review-evidence/house-load-year-comparison-2026-11-15/compare.py`. The PNG is a separate presentation of the hourly CSV.
