# Provider meter versus SigenStor: 2026-11-15

Read-only comparison of the current repository-root `solar.db` with the supplied `HDF_calckWh_10306341332_15-11-2026.csv` in Downloads. No application code, configuration or database records were changed; no application database opener or migration was invoked.

## Result

SigenStor's recorded grid energy agrees closely with the supplied electricity-meter readings over the common history. Differences below are **database minus provider**, in kWh.

| Matched half-hours | Provider import | DB import | Import difference | Provider export | DB export | Export difference |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3,906 | 1,634.437 | 1,635.190 | +0.753 (+0.0461%) | 953.811 | 953.670 | -0.141 (-0.0148%) |

The common window starts **2026-08-22 15:00 Europe/Dublin** and ends **2026-11-12 00:00 Europe/Dublin**, with two missing provider intervals excluded. This is 1,953 matched hours. Database coverage starts at 2026-08-22 14:36 local and ends at 2026-11-15 09:40 UTC; the earlier partial half-hour and dates after the HDF cutoff cannot be compared.

For the **80 complete provider days**, 2026-08-23 through 2026-11-11 excluding September 29, import is 1,613.458 versus 1,614.200 kWh (+0.742; +0.0460%); export is 944.579 versus 944.480 kWh (-0.099; -0.0105%). Complete provider days can still contain collection gaps.

## Differences by calendar month

These are matched portions of each month, not full-month totals where coverage is incomplete. Intervals ending at midnight belong to the preceding day's consumption.

| Period | Provider import | DB import | Difference | Provider export | DB export | Difference |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Aug 22, 15:00–Aug 31 | 79.646 | 80.300 | +0.654 (+0.821%) | 125.663 | 125.700 | +0.037 (+0.029%) |
| September, excluding missing hour | 505.190 | 506.810 | +1.620 (+0.321%) | 461.804 | 462.130 | +0.326 (+0.071%) |
| October, including repeated DST hour | 727.741 | 727.240 | -0.501 (-0.069%) | 332.566 | 332.120 | -0.446 (-0.134%) |
| Nov 1–11 | 321.860 | 320.840 | -1.020 (-0.317%) | 33.778 | 33.720 | -0.058 (-0.172%) |

The small overall discrepancy includes cancellation between months. These observations do not support applying a fixed calibration factor.

## Interval accuracy and gaps

- All 121,535 raw samples were inspected. Neither grid counter is missing or decreases; every stored import/export period delta after the first row matches the consecutive cumulative readings. This history does not exhibit the transient-zero recovery risk recorded in [findings](findings.md).
- Every existing raw hourly/tariff-band group matches its hourly import/export energy aggregate exactly in integer storage units. This verifies grid-energy rollups for this database, not all hourly fields.
- Counters advance in 0.01 kWh steps in this history, whereas provider values have 0.001 kWh resolution. Small half-hour differences and zero-versus-small-export readings are therefore expected; resolution alone does not establish the cause of every discrepancy.
- There are 26 sample intervals longer than 120 seconds in the common window, affecting 44 matched half-hours. Counter deltas retain the energy across gaps, but its precise timing within a gap is unknown.
- Across the 3,862 matched half-hours without those gaps, mean absolute import/export errors are **0.00372 / 0.00327 kWh**; largest absolute errors are **0.080 / 0.031 kWh**. Their total differences remain +0.710 / -0.109 kWh, so close agreement does not depend on the gap allocation.
- With gap intervals included, the largest half-hour import error is +0.305 kWh ending August 31 at 23:00; its adjacent half-hour is -0.299 kWh. These intervals overlap a 41-minute collection gap, so their individual allocations are uncertain. The largest export error is -0.199 kWh ending August 22 at 17:00, also overlapping a long gap.
- The largest complete-day import difference is **November 3: 28.586 provider versus 28.310 DB kWh**, -0.276 kWh (-0.966%). The largest complete-day export difference is **November 8: 0.103 provider versus 0.230 DB kWh**, +0.127 kWh. Both days have no sample gaps over 120 seconds. The relative export discrepancy is large because the energy is small; its cause is not established here.

## Timestamp and comparison method

1. The CSV explicitly labels values as **Active Import/Export Interval (kWh)**, so values were used directly as energy, without multiplying by half an hour. ESB Networks describes its downloadable smart-meter files as HDF CSVs ([official explanation](https://www.esbnetworks.ie/customer-support/customer-support-queries/meter-reading/my-energy-consumption/details/my-energy-consumption/what-file-types-are-supported-to-download-the-smart-meter-data)).
2. `Read Date and End Time` was interpreted as the end of the preceding half-hour in configured `Europe/Dublin` time. The file repeats 01:00/01:30 on October 25, 2026; these are distinct DST intervals, preserved using chronological file order and timezone folds, not deduplicated. October 25 therefore has 50 half-hours. Timestamp interpretation is additionally supported by the alignment check below; the file itself has no UTC-offset column.
3. Both provider streams lack intervals ending **September 29 at 21:00 and 21:30 local**. These were excluded, not filled with provider zeros. SigenStor's two cumulative counters are unchanged over that missing hour. August 22 and September 29 are the only incomplete provider days inside the matched date span.
4. Raw SQLite measurements were divided by 10,000. Consecutive cumulative import/export differences were allocated proportionally by elapsed seconds where they crossed a half-hour boundary. Whole deltas were also summed by ending sample as a sensitivity check; the overall totals are identical here. Long-gap allocations remain estimates.
5. The common-window cumulative endpoints independently give import `1740.94 - 105.75 = 1635.19` and export `1226.00 - 272.33 = 953.67` kWh. Because the missing provider hour has no recorded counter movement, these equal the matched totals.
6. Moving provider alignment by ±30 minutes increases mean absolute error to approximately 0.204–0.206 kWh import and 0.145 kWh export; ±60 minutes gives approximately 0.328 / 0.216 kWh. Unshifted clean-interval errors are 0.00372 / 0.00327 kWh. This strongly supports the chosen end-time/local-time alignment and gives no evidence of a general half-hour or DST-offset error.
7. As a separate diagnostic, trapezoidal integration of signed grid power, split at zero crossings and omitting gaps over 120 seconds, gives 1,621.074 / 948.351 kWh over the clean matched half-hours, versus provider 1,621.660 / 949.079 kWh. Instantaneous-power integration is an estimate, separate from the device's cumulative meters.

## Scope and evidence

This validates **grid import/export accounting** against the supplied HDF, not household-load, PV or battery accounting, conversion efficiencies, tariff billing, or installed meter calibration. The household-load/register-boundary questions in the [register review](sigenergy-home-assistant-register-review.md) remain open. The HDF does not identify the cause of the remaining small differences or establish whether individual entries were estimated or subsequently revised.

Local detailed evidence is in `documentation/reviews/system-review-evidence/provider-meter-2026-11-15/` (ignored by Git): `compare.py`, `summary.json`, `half-hour-comparison.csv`, and `daily-comparison.csv`. Reproduce from the repository root with `python documentation/reviews/system-review-evidence/provider-meter-2026-11-15/compare.py`; it uses SQLite `mode=ro` and `query_only`, standard-library calculations, and the original Downloads path. Auxiliary read-only checks verified raw/hourly equality, cumulative endpoints and timing sensitivity. No model simulations or application test suite were run for this data review.

CSV SHA-256: `2767e73ac78d1cdf22e0376bc3e2f24457eec1d55a2fabd6fdacd3eb2543987e`. The database contained schema version `0.3.3`; results describe the local history ending 2026-11-15 09:40 UTC, not later live collections.
