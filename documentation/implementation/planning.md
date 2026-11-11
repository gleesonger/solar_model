# Planning inputs and results

The only plan model is Markov DP. Read the [Markov implementation](markov-dynamic-programming.md) for its stochastic recurrence, command ordering, physical constraints and report assumptions. The [dynamic programming guide](dynamic-programming.md) covers grid and command resolution.

## Service and input ownership

`services.planning.plan_as_of()` reads device metadata and battery state known at the requested UTC cutoff, assembles the current baseline forecast, creates nine named solar/load combinations and directly calls `plan_optimisation_dynamic_programming.simulate_energy_plan()`. There is no model dispatcher or solver warm-up. The numerical planner has no storage/network/UI dependency.

`storage.queries.planning` selects as-of battery/device data, forecast vintages and the operational house-load profile. Database measurements decode to physical units. Forecast target timestamps differ from collection vintages; future observations do not enter model inputs. Operational calibration and configured-local training boundaries remain as described in the [forecasting guide](forecasting.md).

`generate_plan_report(config, as_of, energy_plan_override=None, *, include_actuals=True)` validates a complete request-local configuration, generates the Markov result and closes storage on success or failure. Overrides do not mutate global settings. Background publication passes `include_actuals=False`; demonstrations may initially include actuals through their cutoff. Model metadata is `markov` and includes the timezone used for display.

## Numerical and report contracts

Required settings are the flat `energy_plan.optimiser` block: Numba selection, transition matrix/reference hours, full-horizon timestep array, grid percentage and power divisions. Legacy model selectors, nested blocks and refinement stages are rejected. Python/Numba share the same recurrence and physical simulator. The first action lasts fifteen minutes, then UTC-aligned durations cover the configured total horizon, split at the visible boundary. Tariffs and user limits are sampled at each interval's configured-local start.

`SimulationResult.expected_score` is the Markov objective. `expected_results` and `expected_lookahead_results` contain deterministic probability-propagated expectations. The nine named `results`/`lookahead_results` frames are fixed-condition stress illustrations of the same policy; their weighted average is not the stochastic expectation. Only the first command is shared before uncertainty is observed. All report frames preserve chronological UTC endpoints, physical energy routes, net-grid billing and cost/protection/limit/terminal score contributions. Advanced includes lookahead; Compact uses the visible portion.

`result_frame()` reports `inverter_dc_to_ac_kwh` as total pre-conversion DC input: `(solar_to_load_kwh + solar_to_grid_kwh) / inverter_dc_to_ac_efficiency + battery_to_inverter_kwh`. Solar routed into battery charging is excluded. Delivered AC plus `inverter_dc_to_ac_loss_kwh` equals this input. This is a report calculation; it does not alter policy selection, battery states, grid billing or scores. Existing saved snapshots retain their original values.

## Actuals versus saved projections

Saved snapshots contain projection data, settings and display metadata, not duplicated telemetry. `services.plan_history.read_report_actuals()` queries physical-unit power and SOC from snapshot-local midnight through the earlier of now and the projection horizon. `load_plan_actuals()` averages onto a UTC minute grid, splitting signed grid power before averaging. SOC times the original nameplate battery capacity gives stored battery kWh. Missing values remain null; chart actual lines join available readings without changing data. Historical actuals may include later corrections and overlap the original forecast. They never feed back into the saved projection.

See [saved plans](plan-history.md) for retention, archived YAML, actuals refresh and snapshot selection, and [configuration](../overview/configuration.md) for strict validation and migration.

## Developer tools

`python -m solar_model.tools.plan_as_of` exports the same Markov result and optional expected frames. `python -m solar_model.tools.benchmark_planning --config config.yaml --cap-seconds 5 --output markov-benchmark.json` measures one isolated synthetic Markov case, including first-use compilation, and terminates work exceeding the deadline. It has no model-selection, independent-scenario, LP comparison or refinement switches. Run simulations/benchmarks only when explicitly requested. Historical investigations under `reviews/` describe retired implementations, not supported commands or current measurements.

## Detailed input assembly

## 2. DeviceInfo from text change history

`load_device_info_as_of()` inspects whether `variable_name` exists, queries device rows ordered descending ID, and filters parsed UTC timestamps to ≤ cutoff in Python. It keeps the first value for each stable identifier. Missing machine names are derived from human variable text; unit suffixes are appended when needed (`kw`, `kwh`, `kva`, `v`, `hz`, `percent`). Numeric text becomes float; other text stays string.

`config.additional_device_info` then overrides values, providing the four inverter and battery conversion efficiencies. Missing required `DeviceInfo` metadata raises an error, even when informational to the solver. The newly collected `grid_max_import_power_kw` is optional for historical compatibility: before its first available reading it defaults to positive infinity (unknown/unbounded), rather than zero or a future reading. Once available, the latest reading at or before the cutoff applies throughout the projection, including zero.

| DeviceInfo fields | Solver use |
| --- | --- |
| `system_battery_capacity_kwh`, `battery_state_of_health_percent` | Usable capacity = product /100 |
| `inverter_battery_charge_power_kw`, `inverter_max_absorption_power_kw` | Separate total battery charging-input limit and grid-to-battery AC absorption limit |
| `inverter_battery_discharge_power_kw`, `inverter_max_active_power_kw` | Discharge energy limit and combined inverter-flow constraint |
| `grid_max_import_power_kw` | Grid-point import cap from 40040; both system-limit helpers expose duration-scaled `max_grid_import_kwh`. Both DP and MILP enforce it. |
| `inverter_ac_to_dc_efficiency`, `inverter_dc_to_ac_efficiency`, `battery_charge_efficiency`, `battery_discharge_efficiency` | Four distinct conversion stages and report losses |
| Model/serial/firmware, rated power/apparent power, grid ratings, pack/string/MPPT counts | Required metadata but no direct optimisation constraint |
| Battery discharge SOC cutoff | Does not shift protection scoring; the controller target is distinct from this device metadata field. |
| Battery charge SOC cutoff and `maximum_pv_input_power_kw` | Required metadata, not newly imposed as hard bounds by the scoring change |

`load_battery_state_as_of()` reads the latest full electrical ORM row with collection epoch ≤ cutoff. It requires SOC and returns `DeviceState(battery_kwh = nameplate_capacity * SOC /100)`. It does not initialise from plant available-discharge energy or apply SOH to this starting value.

## 3. One forecast vintage and its energy dictionary

`load_forecast_as_of()` queries the maximum collection time ≤ cutoff and then all rows for that collection ordered by target. It does not search older complete vintages if that collection is partial.

`interval_energies(rows,panel_ids,zone,source)` extracts non-null cumulative Wh for each configured panel. If any panel has no points, it returns `None`. The caller tries adjusted for all panels, then raw for all panels **in the same collection**; it never mixes sources panel-by-panel.

Each panel's sorted cumulative Wh resets by local target date. First value becomes initial energy; later differences clip decreases to zero; divide by 1000; sum panel contributions into:

```python
dict[datetime, float]  # aware UTC target timestamp -> site interval kWh
```

The planner treats those timestamps as interval **ends**. For retained targets ≥ as-of, first start is target minus `config.forecast.solar.interval_seconds`, then subsequent starts are the preceding retained end. This uses provider collection interval as an initial width; it differs from the dashboard's start-hour interpretation.

UTC dictionary keys preserve distinct autumn fold targets; cumulative resets still use the configured local date. Forecast intervals, scenario generation, projection arithmetic and simulation result timestamps are UTC. The model uses ordinary timedelta arithmetic; tariff/scheduled-limit lookups convert interval starts to configured-local time. Storage-side input assembly retains its local-date handling, and the CLI converts result timestamps for local display.

Solar coverage is clipped/padded to the whole UTC hours surrounding the visible projection plus its 24-hour hidden extension, matching the house-load coverage; leading/trailing missing provider coverage is initially padded with zero solar. The visible part retains that padding; `_lookahead_solar` replaces the hidden day with today's actual-plus-forecast hourly profile. This is a caller policy: `ForecastData` itself rejects extrapolation or gaps.

## 4. House-load forecast inputs

`load_house_load_intervals()` reads hourly `house_load_kwh_period` using `load_hourly_house_load()` and calls the same pure `house_load.predict_day()` helper as daily collection. It reads current holiday ranges for every plan calculation. `history_start_day()` extends the usual history window when the latest 21 holiday dates extend farther back; no current-day or future observations enter the forecast. Ordinary weekday/weekend fitting excludes holiday observations. Holiday fitting uses a separate recency-weighted holiday sequence blended with a flat 30-day baseline. Planning recomputes from eligible readings rather than saved daily profiles. It uses `forecast.load.model_calibration_parameters` as the single source of load-history tuning. The unused legacy `energy_plan` load-history fields and their validation have been removed. Conversion losses are excluded from forecast demand and applied explicitly by the optimiser.

History ends at the as-of local day's midnight, so neither current-day nor future database observations can leak into the forecast, including forecasts for later projection days. Each target date gets its weekday/weekend profile from this available history. Planning maps the profile to UTC hours, preserving DST, covers whole UTC hours surrounding the projection; condition inputs preserve baseline intervals. The model integrates only the requested projection, prorating its edge intervals. Missing predictions raise a house-load-specific error rather than falling back to the loss-inclusive counter. The numeric `ForecastData.load` field now means household energy demand.

## 5. ForecastData and scenario shapes

```python
ForecastInterval(starts_at=aware_datetime, ends_at=aware_datetime, kwh=0.5)
ForecastData(solar=[...], load=[...])
# Stored attributes after construction:
solar: tuple[ForecastInterval, ...]
load: tuple[ForecastInterval, ...]
```

`ForecastInterval.__post_init__` requires aware timestamps and normalises both endpoints to UTC. `ForecastData` sorts each stream and requires non-empty positive-duration contiguous intervals without overlap. Stored interval comparisons and durations use UTC directly; `sum(start,end)` normalises its query endpoints at entry. `sum(start,end)` returns proportional interval energy, assuming uniform power. It raises if coverage is missing. Repeated local hours remain separate: fold=0 to fold=1 is one elapsed hour.

`create_scenarios(forecast, config.energy_plan.scenario_multiplier)` scales baseline intervals into nine named low/medium/high combinations. It does not redistribute high-case energy between half-hours. Medium/medium is named `med`; others use `loadcase_solarcase`. Markov derives all condition-period amounts from the medium baseline and configured multipliers.

Output contract: `list[ScenarioData]`, with `name: str` and `forecast_data: ForecastData`. Names belong to conditions; forecast data contains only solar/load intervals. Scenario weights and probability fields have been removed. `ScenarioDimensionConfig.items()` fixes low/medium/high order, and Markov probabilities come exclusively from configured transitions and the medium/medium starting state.
