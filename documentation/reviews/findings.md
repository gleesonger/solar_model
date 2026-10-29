# Review findings

Known defects, undecided policies and verification gaps. These items qualify the current subsystem contracts; they are not intended behavior or implemented proposals. See [verification scope](verification.md) for integration limits. Documentation maintenance alone does not establish new verification; the dated targeted checks below state their scope.

## Defects and inconsistencies

| Finding | Evidence / consequence |
| --- | --- |
| First-action command does not determine the predicted dispatch | Self-consumption can export all PV while discharging to household load despite available PV. The mode in `NextAction` is the proposed device command, so this requires a command/flow constraint repair, not a presentation rename. See [material planning checks](#material-planning-checks-2026-10-29). |
| AC absorption incorrectly limits total battery charging | `system_limits()` caps all battery charging at `min(battery charge power, AC absorption power)`, including DC solar charging. Stored metadata reports 8.8 kW battery charging versus 5.5 kW AC absorption; historical near-zero-grid samples show charging above 5.5 kW. A controlled 24-hour comparison changes stored solar by 4.18 kWh and projected visible cost by EUR 0.852. See [material planning checks](#material-planning-checks-2026-10-29). |
| Host timezone leaks into ORM local dates | [solar_model/storage/schema.py](../../solar_model/storage/schema.py) hybrids use host `astimezone()`/SQLite `localtime`; legacy hybrids still expose host dates; active recent/lifetime summaries now use configured timezone . Configured timezone can differ from host, especially Docker. |
| Missing planning rules pass config load | Minimum/maximum schedules default empty and validate only when non-empty, but [simulate_energy_plan](../../solar_model/planning/optimisation.py) calls `tariff_rule` on both unconditionally. |
| Solar interval conventions disagree | Dashboard treats forecast timestamps as starts; [load_forecast_as_of](../../solar_model/tools/plan_as_of.py) uses them as ends and uses collection interval for the first interval width. Planning may shift energy timing. |
| Tariff extraction still uses the old availability column | [check-tariffs/extract_minute_data.py](../../check-tariffs/extract_minute_data.py) queries `inverter_battery_available_discharge_kwh`; current schema renames it to `plant_battery_available_discharge_kwh`. The manual extractor needs reconciliation before running against a migrated database. |
| Historical metric additions remain incomplete | Unrelated new hourly metrics still require an explicit backfill policy; v3 rebuilds the reviewed telemetry statistics, but derived minute PV columns are also not backfilled by the opener. Schema presence does not imply historical populated values. |

## Model and data questions requiring decisions

- **Planning limits (static audit):** optimisation enforces configured interval-end minimum/maximum energy and health-adjusted capacity, but does not independently apply metadata SOC cut-offs as hard energy limits. As-of initial state and dashboard SOC use nameplate capacity; forced-action stop conversion uses health-adjusted capacity. Confirm one convention when state of health is below 100%. There is no general site import/export limit, and full PV-priority/saturation remains unverified. Grid import/export exclusivity is now enforced per interval and scenario. `ForecastData` checks interval coverage but not comprehensive finite/non-negative energy values; as-of selection has no input-age rejection. See [optimisation](../../solar_model/planning/optimisation.py), [system limits](../../solar_model/planning/system.py), [input loader](../../solar_model/storage/queries/planning.py) and [types](../../solar_model/planning/types.py). No application change or hardware test was made in this documentation review.
- **CLI grouped scores:** [tools.reports.group_plan](../../solar_model/tools/reports.py) sums energy and cost columns but retains the first value of each new `*_score` column when combining model periods. Grouped CLI score totals therefore omit later contributions in each output bucket. Direct simulation frames and Advanced dashboard/Excel scores use original periods and are unaffected. Correct CLI aggregation separately before relying on its grouped score totals.

- **Household register boundaries:** upstream prefers General load 30282; local House Load integrates Total load 30284. Common-load counter 30228 excludes EV charging, without proving loss exclusion. Later diagnostic samples found these alternatives identical to current readings; no source switch or backfill is justified yet. See the [register review](sigenergy-home-assistant-register-review.md).
- **Unavailable electrical inputs:** unused phase readings appear to contain all-one placeholders; two negative PV voltage/current placeholders can multiply into positive power. Confirm field-specific sentinel/domain rules before changing decoding or integration.
- **Transient counter-zero recovery:** `MeasurementStorage.save_modbus_sample` suppresses a decreasing-counter delta but updates its baseline to the lower reading. A temporary 100 → 0 → 100.1 sequence produces a false 100.1 kWh recovery interval. This is a verified code-path risk; occurrence in the supplied history was not established by this review. Define genuine reset versus transient-zero handling before repair.

- **Load export naming:** full-data exports still use the autogenerated label for `plant_load_power_kw`, while chart CSV keeps the internal `load` column. Analysis labels this measured power House Load. Recent Live/Today now hides the separate Load row, resolving its mixed household-power/device-counter-energy presentation; Past summaries retain both Load and House Load energy.
- **Load accounting:** one overnight hour averaged 0.337 kW household power while Load counters advanced 0.70 kWh. AC output minus export agreed with household power; the battery-to-AC residual was about 0.35 kW. Counters may include losses excluded from power; exact firmware accounting remains unverified. Preserve integrated House Load separately from device Load counters and existing counter-based economics. See the [register review](sigenergy-home-assistant-register-review.md).
- **Negative adjusted solar:** [operational_model._prediction_fields](../../solar_model/planning/forecasts/solar.py) does not clamp additive residual output. Negative W/Wh is possible by inspection; decide physical bounds and test adverse residuals.
- **Replay parity:** live correction handles future target days in each collection; historical rebuild clears all selected targets but replays only each collection's current local day. Confirm the intended audit coverage before relying on rebuilt future-day vintages.
- **DST representation:** several views use fixed 24 clock-hour buckets, merging repeated hours. Spring DST SQL tests exist; UTC planning arithmetic and both DST transitions now have regression coverage ; fixed-clock dashboard views and broader local display still need separate verification.
- **Interval attribution:** counter gaps use the ending timestamp's tariff/hour and PV integration spans the whole gap. Decide acceptable maximum gap and whether tariff/day boundaries require interval splitting.
- **Availability downloads:** state availability uses the last reading; changing to a mean requires an explicit contract decision. Earlier averaging expectations were not proven numerical defects.
- **Forecast export meaning:** export aggregation averages cumulative `*_watt_hours` while charts difference them into interval energy. Confirm whether downloads should expose stored provider fields or reproduce chart energy.
- **Coarse planning lookahead:** the hidden 24-hour extension uses six four-hour slots with interval-start tariffs/limits and aggregated solar/load. It deliberately does not split at tariff boundaries; within-slot cheap charging, demand/solar ordering and changes in limits are approximated. The hidden solar day now repeats today's actual-plus-adjusted hourly profile; this assumes similar solar conditions, and sample means approximate energy when collection is irregular. Missing actual hours fall back to the selected forecast, whose uncovered edges still pad to zero. The hidden horizon and terminal credit reduce depletion incentives but do not remove these coarse-slot approximations. See [plan model](../overview/plan-model.md#hidden-lookahead).
- **Scenario interpretation:** first-action controls are shared across scenarios; later decisions are independent. This assumes full scenario-specific information after the first interval and is optimistic about future flexibility.
- **Plan model implementation decisions:** the [current planning reference](../overview/plan-model.md#63-remaining-limitations-and-execution-boundary) documents the implemented shared next action, production forecast reuse, 0.5/1/1.5 scenarios and tariff-scaled score reporting. Planning remains report-only. Remaining decisions concern physical command response/handover, consistent SOC conventions, input policies and numerical tolerances.
- **Validation policy:** unknown YAML keys are ignored and some numeric/site fields lack bounds. Decide whether descriptive keys should be formal fields and other unknown keys rejected.

## Coverage and maintenance gaps

- Chrome Android saved offline pages can preserve dashboard visuals while in-page scripts are inactive. Finite CSS activity indication and an absolute Last reading timestamp reduce ambiguity, but actual saved-page/freeze behaviour still needs verification on Android hardware; desktop Chromium regression checks do not establish it.

- Runtime dependencies remain ranged, with no lockfile. HiGHS is now explicit; package extras declare test, browser and research dependencies.
- Tariff helper logic is duplicated between config validation and economics; changes must preserve matching parity.
- `solar_model/storage/queries/dashboard.py` retains older ORM/pandas calculations used by current adapters or SQL regression comparisons. Remove individual paths only after checking both consumers and independent test references; the unmounted Hourly tab has been removed.
- Independent `check-tariffs` scripts run on import and extraction has a hard-coded external database path; treat them as manual tools.

## Suggested repair order

1. Correct the demonstrated command/dispatch mismatch and battery/AC charging-limit separation below.
2. Resolve forecast interval meaning and required planning-rule validation; extend replay parity and adverse optimiser coverage before changing those contracts.
3. Review dependency reproducibility and retained legacy paths; HiGHS declaration and Docker port metadata have already been corrected.


## First-step action modelling

`simulate_energy_plan` derives first-step operating mode from mutually exclusive forced battery rates. SOC stopping is modelled, including starting beyond the cut-off; self-consumption ignores action SOC cut-offs. The fixed 100 W minimum positive rate distinguishes forcing from zero. PV-first priority and saturation at other equipment/flow limits from the target plan model are not yet fully enforced; scenario flows remain optimisation decisions within the existing equations. Installed-firmware response is also unverified.

Planning remains report-only: no device commands, acknowledgement, EMS ownership or handover are implemented. Real device/provider behavior, Docker installation, actual process-kill recovery, sustained concurrent SQLite writes and exhaustive browser/output combinations remain unverified. Dashboard administrative confirmation is not authentication; intended access policy remains undecided.

## Material planning checks (2026-10-29)

Targeted review of the current working tree, including its uncommitted scenario/configuration changes. The numerical cases below use synthetic in-memory forecasts, the current YAML tariff schedules and 95% charging/discharging efficiencies. They are counterexamples, not historical closed-loop savings measurements. The user's database was opened only through SQLite `mode=ro` to inspect metadata and existing telemetry. The review used no migrations or device commands. Its grid-meter accounting finding has since been corrected with per-interval/scenario direction constraints; the remaining findings below concern command response and battery charging limits.

### Proposed command and predicted flows disagree

Source: [first-step constraints and action extraction](../../solar_model/planning/optimisation.py).

A 12:00 UTC start on 2026-10-29, 10 kWh synthetic battery capacity, 5 kWh initial state and 5 kW equipment limits reproduced self-consumption mode 2 with both forced rates zero. Solar is 1 kWh and household demand 0.25 kWh in the first 15 minutes, with zero energy thereafter. Use the shipped 24-hour visible projection plus hidden extension and protection preference zero. The solver sends all 1 kWh PV to grid, supplies the 0.25 kWh household demand from battery, and ends at 4.736842 kWh. The missing PV priority lets battery-to-load substitute for the disabled battery-to-grid route.

Under the documented self-consumption policy, this unconstrained, partially charged case instead supplies demand from PV and charges from its surplus: 5 + (1 - 0.25) * 0.95 = 5.7125 kWh. The difference is about 0.976 kWh in one action interval. Intentional discharge to prepare for cheaper charging can be economically reasonable, but the proposed command must express that intent and reproduce the predicted rates/stopping behaviour. Correct the mode-dependent dispatch constraints; merely renaming mode 2 after solving is not an established fix. Installed-firmware response was not tested.

### Battery DC charging is restricted by an unrelated AC limit

Source: [system_limits](../../solar_model/planning/system.py) and [battery-charge constraint](../../solar_model/planning/optimisation.py).

`max_charge_kwh = min(inverter_battery_charge_power_kw, inverter_max_absorption_power_kw) * duration` bounds the sum of solar and grid charging inputs. The AC absorption constraint is also applied separately to grid charging. This incorrectly restricts DC-coupled solar charging to the AC limit.

Read-only historical evidence: at **2026-08-30 14:29 UTC**, stored plant readings show battery charging **7.782 kW**, PV **8.081 kW**, grid import **0.030 kW**, household load **0.269 kW**. There are 41 stored samples with battery charging above 5.5 kW and absolute grid power below 0.1 kW. These observations establish that total charging is not universally limited to 5.5 kW; they do not establish a complete efficiency curve or atomic register balance.

Controlled synthetic comparison: start at 2026-10-29 12:00 UTC, capacity 18.08 kWh, initial energy 0.55 kWh and the metadata/efficiencies above. Supply 8 kW solar for two hours, then zero solar; demand is 4 kW at clock hours 17-21 on each day and 0.3 kW otherwise. Retain the shipped 24-hour projection plus extension. A single-scenario diagnostic changes only the total battery charging bound to its 8.8 kW metadata value; the separate AC absorption and inverter-flow constraints remain active. This conservative diagnostic is not a complete battery-side power/efficiency convention repair.

| Result | Current model | Separated battery/AC bound |
| --- | --- | --- |
| Solar stored over first two hours | 10.45 kWh | 14.63 kWh |
| Solar exported over first two hours | 4.4 kWh | 0 kWh |
| Visible-day net cost | EUR 4.64353 | EUR 3.79150 |

The EUR 0.85202 difference is about 18% of the current projected visible-day cost. It survives protection preferences zero and 50. Both noon cases return self-consumption initially, so that comparison alone does not establish a different first command or measured real-world savings.

A separate morning comparison does change the immediate command. Start at **07:00 UTC** on the same date with **3 kWh initial energy**, 8 kW solar at **12:00-14:00** on the first day only, and household demand of **3 kW at 17:00-21:00** on both days and **0.3 kW otherwise**. Keep protection preference **50**, the shipped projection, tariffs, battery/inverter metadata, efficiencies and terminal credit. The current bound produces **mode 4, 5.5 kW forced charging** in the first 15 minutes (1.375 kWh grid energy for battery charging, plus household import). Changing only the battery/AC bound separation produces **mode 2, no forced charging and no grid import** in that interval. Projected visible costs are respectively EUR 1.71142 and EUR 1.59229. Thus the incorrect capability bound can cause unnecessary immediate grid charging as well as substantially pessimistic solar-storage projections. These are model comparisons, not measured executed-device savings.

The repair should keep battery-port limits separate from AC conversion limits and explicitly reconcile which side of efficiency each rated power constrains.

### Scope and exclusions

Completed counterexample solves returned optimal status and each finished within the repository's five-second simulation limit. These targeted checks do not establish annual savings or installed-firmware response. Grid import/export regression coverage is maintained in `tests/planning/test_grid_exchange.py`; see the implementation guide for its current direction/billing contract.

Fixed scenario weights, two-stage recourse, reserve utility, terminal valuation and coarse hidden slots remain documented design choices. This review does not establish that replacing them improves decisions. SOC/nameplate versus health-adjusted capacity remains inconsistent, but the inspected installation metadata reports 100% health, so it was not promoted as a material current-installation finding. Forecast timestamp disagreement, site-limit settings and stale-input policy require separate evidence before claiming a quantified economic or system-capability failure.
