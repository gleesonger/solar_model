# Economics and energy planning

For as-of queries, device/profile/interval structures, solver variables/equations, and output columns, see [planning implementation](../implementation/planning.md).

## Stored economics

Source: [solar_model/tariffs.py](../../solar_model/tariffs.py), `economic_period_values`.

- Import cost = interval grid-import kWh × import rate.
- Export revenue = interval grid-export kWh × export rate.
- Net cost = import cost − export revenue.
- No-solar/battery comparator = measured load kWh × import rate; it excludes standing charges.
- Collection stores rates/band/costs at that time. Editing YAML does not retroactively change stored history.
- [solar_model/tools/recalculate_economics.py](../../solar_model/tools/recalculate_economics.py) replaces selected historical values and rebuilds hourly aggregates; see [operations.md](../operations/runbook.md).

## Optimiser contract

Source: [solar_model/planning/optimisation.py](../../solar_model/planning/optimisation.py), `simulate_energy_plan`.

- Inputs: `DeviceInfo`, starting battery kWh, named solar/load interval forecasts, configuration, projection start.
- `ForecastData` sorts intervals and rejects gaps, overlaps, invalid lengths, and uncovered query ranges. Overlap energy assumes uniform power within each supplied interval.
- Each result is a scenario DataFrame containing interval bounds, tariffs/limits, energy routes/losses, battery levels, and economics.
- CVXPY solves a **mixed-integer** problem with HiGHS. Boolean charge mode prevents simultaneous battery charging and discharging.
- Energy routes include solar to load/battery/grid/clipping, battery to load/grid, and grid to load/battery.
- Battery capacity is nameplate capacity × state of health; charge/discharge and inverter/absorption powers become interval kWh limits.
- Scheduled battery minimum/maximum apply at interval end; maximum is capped by adjusted capacity. Efficiencies add explicit charge/discharge losses.
- Device SOC cutoff fields and `maximum_pv_input_power_kw` are required metadata but are not enforced by the optimiser; configured kWh schedules set the battery reserve/ceiling.
- Objective combines average negative net cost with weighted battery-level worth accumulated over the horizon (`battery_end_kwh × import_rate × interval_hours`). It is not solely a terminal reserve value.
- Scenarios have independent decisions and no shared/non-anticipative constraint. Jointly solving them does **not** produce one robust schedule that can be applied across all scenarios.
- Solver infeasibility raises an error; output solar/load balances are checked to 0.001 kWh.

## As-of workbook

Source: [solar_model/tools/plan_as_of.py](../../solar_model/tools/plan_as_of.py), a CLI rather than automated tests.

- Reads the newest device metadata, battery sample, and forecast collection known by the cutoff; it avoids `open_database()` migration side effects.
- The cutoff applies to database observations and forecast vintages. Efficiencies, tariffs, and planning settings come from the supplied configuration; retain that configuration to reproduce a historical run.
- Starting battery uses nameplate capacity × SOC, unlike dashboard available-discharge energy.
- Forecast prefers adjusted cumulative Wh. If any configured panel has no adjusted points, all panels fall back to raw from the same collection; it does not mix sources or search older collections. Missing leading/trailing coverage is padded with zero solar.
- Load profile uses previous local days only, exact weekday/hour averages, configured recency weights (shipped 42-day window/21-day half-life). Missing slots raise an error.
- Creates nine low/base/high load×solar combinations, multipliers 0.5/1/1.5. High solar shifts paired half-hours to 25%/75%; high load uses 80%/20%.
- Durations, overlap and projection steps use UTC elapsed time across DST; tariff interpretation remains local and repeated output hours retain their offsets.
- Writes one sheet per scenario and a Summary. Coarser output sums energy/costs, preserves opening/closing battery levels, and combines limits conservatively.
- Pass an explicit timezone offset and hour-aligned cutoff. Scenario construction requires half-hour-aligned, whole-hour coverage; the current solar interval convention differs from dashboard labels.
- The CLI deletes an existing output workbook before solving. Select an expendable output path.

## Independent tariff experiments

- [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) reads a hard-coded database path and emits minute CSV/actuals workbook.
- [check-tariffs/model_tariffs.py](../../check-tariffs/model_tariffs.py) compares enabled JSON tariffs/plans, includes standing charges, and writes Excel summaries. It uses a separate rule-based model and Wh/cents units.
- Both execute top-level work on import; run intentionally from their working folder after inspecting paths. Extraction requires the optional `research` package dependencies (`openpyxl`).

The public calculation entry point is `planning.model.build_plan(PlanningInputs(...), config)`. It returns scenario frames; `services.planning.plan_as_of` loads the cutoff-aware inputs and `tools.reports` handles Excel/grouping. `planning/system.py` and `planning/scoring.py` isolate physical limits and the unchanged objective.
