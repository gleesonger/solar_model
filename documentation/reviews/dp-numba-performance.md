# DP Numba performance

Measured 2026-10-31 using synthetic inputs on this Windows 11 laptop, Python 3.12, NumPy 2.5.2 and Numba 0.68.1. These results describe the shared numerical implementation's interpreted and compiled backends, rather than the earlier object-based Python implementation. They are local observations, not deployment guarantees.

This is the historical warmed implementation. The code was subsequently combined into one DP module with `use_numba` and a local optional import. Warm-up was removed at the user's request. The packed-array implementation was then replaced by a smaller overlay around the original functions using named tuples and typed lists; the timings below have not been remeasured for that version. Current first-call timings include lazy import/cache loading/compilation, so the measurements below do not describe current first-call latency.

## Configuration and timing

Each solve used nine weighted scenarios, 96 visible quarter-hour periods and six hidden four-hour periods. Both backends used the complete configured sequence:

```text
(2% /5, full range)
-> (1% /10, +/-5 percentage points)
-> (1% /20, +/-3 percentage points)
-> (1% /40, +/-1 percentage point)
```

Each observation used a fresh worker, run sequentially in order Python, Numba, Numba, Python, Python, Numba. The worker warmed both compiled kernels before the ready/start handshake and simulation timer. Timed simulation includes preparation, array packing, all four stages, shared-command selection and pandas reports. Additional report validation is shown separately in the total column. Process startup, imports and backend initialisation/compilation are excluded. No timed run created additional compiled signatures. Every run had a 120-second simulation deadline and completed successfully.

## Results

| Backend | Run 1 simulation | Run 2 simulation | Run 3 simulation | Median simulation | Median including validation |
| --- | ---: | ---: | ---: | ---: | ---: |
| Python | 19.675 s | 15.329 s | 15.243 s | 15.329 s | 15.378 s |
| Numba | 0.824 s | 0.575 s | 0.555 s | 0.575 s | 0.622 s |

The ratio of median simulation times is **26.6 times faster**. Separately measured Numba initialisation/cache loading took 0.621, 0.311 and 0.357 seconds. Those observations had a populated compilation cache; they do not establish cold first-compilation latency. Actual first-call latency includes initialisation even though these performance comparisons exclude it.

All six runs had exactly matching per-scenario score components, net costs, battery endpoints, first-step reported flows and selected command. Their probability-weighted outcomes were:

| Measurement | Python | Numba |
| --- | ---: | ---: |
| Full-horizon score | 5.410881598 | 5.410881598 |
| Visible 24-hour net cost | EUR 0.437229054 | EUR 0.437229054 |
| Full 48-hour net cost | EUR 0.312382617 | EUR 0.312382617 |
| Battery at 24 hours | 14.882114961 kWh | 14.882114961 kWh |
| Battery at 48 hours | 18.000000000 kWh | 18.000000000 kWh |
| First command | Self-consumption | Self-consumption |

Validation checked energy balances, ratings, battery/export limits, exclusive charge/discharge and grid import/export, interval lengths and report cost arithmetic across visible and hidden periods. Targeted regression checks additionally compare backward tables, feasibility, bounded forward paths and complete small-horizon reports between backends. Numba accelerates the same approximate DP search; it does not change its grid, commands or scoring rules.

## Commands and historical evidence

Install the compiled path with `python -m pip install -e ".[numba]"`. These commands still select equivalent configured solves, but the current benchmark includes lazy startup and therefore has a different timing scope from this warmed comparison:

```powershell
python -m solar_model.tools.benchmark_planning --model dp --grid production --cases baseline --dp-backend python --cap-seconds 120 --output dp-python.json
python -m solar_model.tools.benchmark_planning --model dp --grid production --cases baseline --dp-backend numba --cap-seconds 120 --output dp-numba.json
```

The historical records used `simulation_seconds` for warmed model runtime and `total_seconds` for runtime plus validation; `initialisation_seconds` was excluded and signature counts were checked before/after simulation. Current records instead mark `numba_initialisation_in_timing: true` and do not warm or exclude it. Generated historical evidence is under ignored `documentation/reviews/system-review-evidence/dp-numba-{python,compiled}-{1,2,3}.json`; this report retains the measurements independently of those artifacts.
