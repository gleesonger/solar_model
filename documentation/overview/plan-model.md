# Plan model

Markov dynamic programming is the only planning optimiser. It proposes a next battery-control action and produces projections; the application does not send battery-control commands. The headless planning service schedules recalculation from committed readings. The numerical model has no storage, network or UI dependency.

Sources: [planner preparation and physics](../../solar_model/planning/plan_optimisation_dynamic_programming.py), [Markov policy](../../solar_model/planning/markov.py), [transitions](../../solar_model/planning/markov_transitions.py), [service](../../solar_model/services/planning.py), and [implementation contract](../implementation/markov-dynamic-programming.md).

## 1. Policy and uncertainty

The state is time, battery energy and the current solar/load condition. Nine low/medium/high combinations share one full-range battery grid. The starting condition is medium/medium with probability one. Each candidate command is selected before the interval's condition is known; later decisions can respond to conditions assumed observed after interval completion. Solar/load transitions are independent and use their separate `optimiser.transition_matrix.solar` and `.house_load` matrices with the configured shared reference duration. See the Markov guide for night-time observability and interpolation limitations.

The optimiser chooses the expected-value policy, not an average of independently optimal scenario paths. Every supported outcome must obey physical capacity and equipment/grid limits. User battery schedules remain soft penalties. `expected_score`, expected visible/hidden frames and nine fixed-condition stress illustrations are separate outputs. The stress paths share the first command but do not enumerate all changing-condition futures; their weighted mean is not the Markov expectation.

## 2. Configuration and time

All six fields in the flat `energy_plan.optimiser` block are required: `compile_model_to_native_code`, `transition_hours`, `transition_matrix`, `projection_resolution_mins`, `grid_step_percent`, and `power_divisions`. There is one full-range solve. Model/uncertainty selectors, nested solver blocks, MILP and path refinement no longer exist. Legacy settings fail validation when used to run a model; archived snapshot settings remain viewable unchanged.

The first action always lasts fifteen minutes. Subsequent intervals align to UTC clock boundaries, split at the configured visible cutoff and stop at the total elapsed horizon. Tariffs and schedules are sampled at interval starts in the configured timezone. Solar/load forecast energy is integrated over actual interval durations. The duration array must cover the entire horizon explicitly.

## 3. Scoring

Cost contributes negative net grid cost. Battery protection uses the exact diminishing-return function on stored energy measured from zero. The maximum reward scales with `protection_preference / 100`, preferred reserve, the time-weighted reference import price over the next 24 elapsed hours and projection duration divided by 24. User bounds add bounded soft penalties; hard physical constraints do not become penalties. Final battery energy receives terminal credit using deliverable efficiency and the mean hourly import/export midpoint over the following twelve hours. Markov backward values and forward probability-propagated report totals must reconcile.

## 4. Forecast Models

### 4.1 Overview

Forecasts supply interval solar and house-load energy for the planning horizon, excluding conversion losses already modelled separately. Produce each baseline once, then create variants using simple scalar multiplication; do not refit the models separately for each variant.

The configured solar and house-load low/medium/high multipliers define condition magnitudes. `create_scenarios()` scales each original forecast interval without high-case timing redistribution. Names are `load_solar`, except medium/medium is `med`. The required `energy_plan.scenario_multiplier` block contains scalar low/medium/high multipliers; scenario probabilities have been removed. Markov decisions use the transition matrix and start at medium/medium. No independent-path probability-weighted objective remains.

The existing forecast parameter names are `lookback`, `halflife`, and `prior_effective_observations`, under `forecast.solar.model_calibration_parameters.day_ahead`/`intraday` and `forecast.load.model_calibration_parameters`. Despite its name, the last parameter is a decimal influence fraction $f$, not an observation count. The configured fraction f gives prior strength $a=1/f-1$; evidence n then receives influence $n/(n+a)$. Here n is the matching-day weight sum for load, or the effective sample size for solar intraday.

### 4.2 Solar Day-ahead Forecast

We look at where the provider's forecast has historically been too high or too low at different times of day, and correct for that bias. This gives the day-ahead baseline before any same-day observations are used.

Select the last Forecast.Solar snapshot at or before local midnight; a new installation without one uses its first same-day snapshot. Preserve raw forecast history and collection/target timestamps.

For each UTC hour, fit a scaling factor through zero using earlier target days. With age and configured `halflife` both in days, each observation has weight $w=2^{-age/halflife}$. Here raw is the provider forecast power and actual is measured PV power, both in kW. The factor is $\sum w\,raw\,actual/\sum w\,raw^2$; missing usable training gives factor 1. These observation weights are forecasting calibration weights, distinct from Markov transition probabilities. Interpolate factors around hourly centres and apply them to the site solar forecast, allocating output to panels by raw-power share.

Current YAML uses 42-day lookback and 14-day half-life; all calibration values must be explicitly supplied; missing values fail validation. `prior_effective_observations` is null because this model has no prior-shrinkage parameter. These are calibration inputs, not separate scenario controls.

### 4.3 Solar Intraday Adjustment

The intraday adjustment estimates whether today is producing more or less solar than the day-ahead forecast expected, then carries that tendency into the remaining forecast. If recent completed hours have underperformed, it lowers the forecast; if they have outperformed, it raises it.

Recent observations matter more than older ones. Differences are judged relative to the usual variation at each hour, so observations from different times of day can be combined. The adjustment is cautious when evidence is limited and gains influence as evidence accumulates. It estimates an above/below-forecast tendency rather than an explicit percentile.

Current YAML uses a 1-hour half-life, no additional lookback limit within today, and `prior_effective_observations: 0.3333`, giving one effective observation about one-third influence on the correction. Only completed hours contribute; the current incomplete hour is excluded. The current target day uses intraday residuals for the current and remaining hours. Future target days receive the calibrated baseline without today's residual adjustment. Without usable observations, the calibrated forecast is retained.

### 4.4 Daily Load Forecast

We estimate demand from recent days, distinguishing weekdays from weekends. Where matching history is sparse, we borrow information from other days. The result is one hourly demand profile for the day, which stays fixed once issued.

Use the configured lookback and recency weights $2^{-age/halflife}$, with age and half-life in days. For each clock hour, n is the sum of matching weekday/weekend weights; the configured influence fraction f gives $a=1/f-1$ as in section 4.1. Blend the matching-type and all-days estimates with influence $n/(n+a)$. Without matching-type evidence use the all-days estimate; without any hourly evidence leave the prediction missing.

Current YAML uses 42-day lookback, 14-day half-life and influence fraction 0.2, giving prior strength 4. The first scheduled forecast run of a local day saves that day's profile atomically. It remains fixed across subsequent runs and restarts. There is no intraday load model. Stored UTC targets retain the 23/24/25 hours of the local day.

Planning uses the same production `house_load.predict_day()` helper and `forecast.load.model_calibration_parameters` settings, but recomputes a profile for each required local target date rather than consuming the persisted daily profile. It reads current holiday periods on each calculation: ordinary weekday/weekend profiles exclude holiday observations; holiday targets use the separate holiday model with a 30-day flat baseline and half-length holiday-count lookback/half-life. Every target date sees history only before the as-of local day's midnight, so no current-day/future observations leak into tomorrow's projection. Missing predicted hours raise an error. The unused energy-plan load-history fields have been removed.

`load_forecast_as_of` selects the latest provider collection at or before as-of, preferring complete adjusted panel cumulative-energy fields and otherwise raw fields. It converts cumulative Wh into kWh per target using configured-local reset days. The planner currently treats forecast target timestamps as interval ends; this differs from dashboard interpretation and remains a [review finding](../reviews/findings.md). The visible forecast retains its existing leading/trailing zero-padding policy. For the hidden extension, `_lookahead_solar` builds today's actual-plus-forecast hourly mean-power profile and repeats it by local clock hour, replacing the provider tail. Actuals use only samples known by as-of; an elapsed hour without samples falls back to its saved forecast and logs a warning. ORM PV kW samples are averaged within each hour; the elapsed/future fractions of an open hour use actual/forecast respectively. Repeated autumn source hours are averaged over their elapsed duration; a skipped spring source hour contributes zero. Solar and house-load coverage surround the visible projection plus the 24-hour extension at full UTC hours so nine scenarios can be built; the model integrates its actual requested intervals. These are explicit input-assembly policies, not ForecastData extrapolation.

`forecast.solar` groups provider endpoint, timeout, scan interval, array definitions and day-ahead/intraday calibration. `forecast.load` groups house-load calibration. Dashboard PV-string mapping uses unique array names, resolved to stable panel IDs for storage queries.

## 5. System Model

### 5.1 Overview

The system model determines whether a proposed action is physically achievable and predicts its effects under each scenario. Every scenario starts from the same measured battery state and device configuration, but receives different solar and load energy.

It represents battery storage, household demand, solar generation, the inverter and the grid connection. Hard constraints describe physical operating bounds; the preferred protection reserve remains a soft objective. No off-grid household load reduction is assumed: protection scores stored kWh from zero, not promised hours of autonomy.

### 5.2 Operating Modes and Command Response

The executable control set is self-consumption (2), charge with PV first (4), and discharge with PV first (5). Modes 3 and 6 are excluded from this design. The numerical mapping is defined in Appendix 6 of the [Sigenergy Modbus V2.9 specification](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf); exact command response still requires verification on the installed firmware.

Markov DP selects among three Sigenergy policies, including zero-power variations of modes 4 and 5. The first interval follows the assumed PV-to-load, surplus-to-battery, then export order, and PV before battery for load. Positive force-charge and force-discharge rates are ceilings constrained by physical supply, power limits and site/battery-export limits. A controller SOC target is derived after solving, not applied as an inverter stop. User minimum/maximum energy schedules remain hard interval-end bounds in Markov DP and soft score preferences in DP.

This is a declared response model, not firmware verification. The active mode persists for the interval; power and energy are averaged uniformly. Ramping, response delay and subinterval clipping are not simulated.

The [Sigenergy Modbus V2.9 specification](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf) and [register review](../reviews/sigenergy-home-assistant-register-review.md) provide integration context. No control-register writes, acknowledgement/read-back, retained EMS ownership or handover implementation exist in this application.

### 5.3 Battery State, Energy Flows and Losses

We track where energy goes and how much remains in the battery, accounting for losses whenever it is charged or discharged.

Battery state is stored energy $B$, with non-negative interval ends bounded by health-adjusted capacity. Markov DP additionally enforces configured minimum/maximum energy schedules; DP scores breaches of those schedules. Protection scores this same stored energy: $E=B$, with zero as the origin for both E and R. The discharge cut-off does not shift the scoring origin. The input loader and dashboard SOC conversion use nameplate capacity times SOC/100; physical capacity and command-stop conversion use capacity times state of health/100. This mismatch needs reconciliation when health is below 100%; available-discharge telemetry has a different meaning and is not substituted. Metadata charge/discharge SOC cut-offs are not automatically added as hard energy bounds.

The solver uses interval kWh for PV energy U at the DC input, battery charging input C at the DC port, battery discharge output D at the DC port, grid charging input G at the inverter AC port, stored battery energy B, and grid import/export. Four separate efficiencies apply: inverter AC-to-DC $\eta_{acdc}$, inverter DC-to-AC $\eta_{dcac}$, battery DC-to-DC charge $\eta_c$, and battery DC-to-DC discharge $\eta_d$. PV is assumed available at the DC input without an additional PV-to-bus loss:

```text
battery_solar_charge = C - eta_acdc * G
gross_ac_output = eta_dcac * (U - battery_solar_charge + D)
inverter_net_ac = gross_ac_output - G
battery_end = battery_start + eta_c * C - D / eta_d
grid_export - grid_import = inverter_net_ac - household_demand
```

Source-to-destination routes are reconstructed for reports: PV charges first, then grid; remaining PV serves household load before battery discharge; excess AC is exported. The four explicit loss columns are `inverter_ac_to_dc_loss_kwh = G(1-eta_acdc)`, `inverter_dc_to_ac_loss_kwh = (U-battery_solar_charge+D)(1-eta_dcac)`, `battery_charge_loss_kwh = C(1-eta_c)`, and `battery_discharge_loss_kwh = D(1/eta_d-1)`. These are model allocations, not separately measured flows. The reported `inverter_dc_to_ac_kwh` is the combined solar/battery DC input before inverter conversion, excluding solar sent to battery charging; it equals delivered AC plus the DC-to-AC loss.

The resulting `battery_end` is $E_{t,s}$ used by the protection curve; `grid_import` and `grid_export` are $I_{t,s}$ and $O_{t,s}$ used by the cost formula. Carry each scenario's end energy into its next interval. The first starting state is measured, not a forecast. Do not allow simultaneous battery charging and discharging or assume that the same battery energy has a different physical identity because it came from PV or the grid.

### 5.4 Physical Constraints

Battery charging input from solar plus grid is bounded by battery DC charging power times interval hours. Gross inverter AC output respects its active output limit; grid charging input respects AC absorption and site import limits. AC absorption does not cap DC solar charging.

Implemented constraints include health-adjusted capacity, configured hard minimum/maximum battery schedules, mutually exclusive charging/discharging, duration-scaled battery limits, signed inverter AC output/absorption bounds and the configured battery-to-grid export limit. The preferred reserve R is separate and soft. Current power settings constrain battery/inverter routes; they do not establish general site import/export meter limits.

Solar may be explicitly clipped when it cannot be used, stored or exported. Flow equalities conserve solar, household load and battery energy. Import/export flows are mutually exclusive within each interval and scenario. The same shared action can import in one scenario and export in another. The collected grid-point import cap limits imports; output capability limits exports.

All three first-step modes follow the declared scenario-specific physical response, including PV priority and forced-power saturation. Configured energy/export limits reject unsafe commands rather than reducing their predicted response; energy stops are encoded in the active command, and restrictive export limits bound its discharge rate. Self-consumption is unavailable when its passive response would breach user energy limits. The battery-only export cap applies to the reconstructed battery export `min(delivered battery output, grid export)`, independently of solar exports. Hardware response and SOC-convention questions remain tracked in [review findings](../reviews/findings.md). Infeasible solves raise an error; initial measured energy is not itself constrained by interval-end minimum/maximum schedules.

### 5.5 Units, Time and Data Conventions

Planning uses physical kWh, kW, hours, EUR/kWh and EUR. Database measurements are integers scaled by 10,000; ORM reads remove that scale, while raw SQL must divide explicitly. Forecast.Solar W/Wh also require their own unit conversion.

Use aware UTC instants and explicit start/end intervals, treating energy as belonging to `[start, end)`. Local dates, tariffs and daily forecast issuance use the configured timezone. The visible 24-hour projection ends 24 elapsed hours after its starting instant, including a partial period when necessary; configured lookahead resolutions complete the 48-hour total horizon. A local day can contain 23 or 25 hours. Resampling must conserve energy and preserve distinct repeated hours.

Only observations and forecast vintages available at the planning cutoff are eligible. Keep raw and adjusted history separate and preserve cumulative-counter reset handling. Unknown data must not silently become observed zero. ForecastData validates aware intervals, ordering, contiguity and requested coverage, and the model validates condition names and configured multipliers. The solver rejects non-finite numeric inputs and negative solar/load energy or battery export limits. Input-age rejection is not implemented; a valid as-of snapshot can still be old. Solar fallback/padding and missing-house-profile behaviour are described in section 4.4 and remain data-policy limitations.


## 6. Validation and open decisions

Targeted checks cover matrix validation, duration scaling, information timing, small exhaustive recurrences, physical transitions, expected-score reconciliation, as-of input assembly, report persistence and browser presentation. Use temporary databases; no benchmark or full simulation is implied by a configuration or syntax check. Remaining limitations belong in [findings](../reviews/findings.md). Historical solver/performance investigations describe retired code, not current commands or measurements.
