# Sigenergy Home Assistant register review

Reviewed 2026-10-26 against [Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus), release 1.3.0, commit `16d710d7348b625521eba789110878c245e1903d` (2026-10-25). References below pin source to that revision. This is a static source/protocol review with read-only checks of the supplied `solar.db`; no integration was installed and no device commands were sent.

## Main conclusions

- Our 33 electrical register definitions match the integration's corresponding addresses, word counts, signedness and scaling. This comparison found no decoding explanation for the battery-to-AC difference.
- Upstream has investigated essentially the same roughly 100 W difference. It preserves the readings and does not compensate for them.
- Household demand, total-load power and total-load energy must remain distinct. The integration's household power source differs from ours.
- A common-load energy counter exists and merits measurement. Neither its name nor the protocol proves it excludes conversion losses.
- Collection quality, register support and counter recovery deserve improvements independently of the load question. Recommendations below are proposals, not implemented behavior.

## Household load and counters

| Address | Documented quantity | Integration behavior | Our current behavior |
| --- | --- | --- | --- |
| 30282 | General load power, S32 / 1000 kW | Preferred input to calculated Consumed Power | Not collected |
| 30284 | Total load power, S32 / 1000 kW | Separate Total Load Power sensor | Stored as `plant_load_power_kw`; integrated into House Load energy |
| 30092 / 30094 | Total-load daily / lifetime consumption, U32 / U64 / 100 kWh | Direct energy sensors | Original Load energy series |
| 30228 | Common-load lifetime consumption, U64 / 100 kWh | Register mapped; no separate entity description in `static_sensor.py` at this revision | Not collected |

The integration prefers 30282; otherwise its household calculation uses AC plant output plus signed grid import plus third-party PV, less AC-charger demand, clamped at zero. Its function's opening PV/battery formula comment is stale: the executable fallback uses AC plant power. The separate General Load entity is commented out because Consumed Power uses that input. It does not integrate household power into a plant household-energy sensor: the plant integration-sensor list is empty. Consequently its household power and direct total-load energy are not necessarily matching measurements. See [calculation and sensor declarations](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/calculated_sensor.py#L350) and [direct sensor declarations](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/static_sensor.py#L228).

The supplied [vendor protocol](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf), physical PDF pages 21–22 (printed 13–14), specifies that 30228 excludes EVAC and EVDC charged energy. It does **not** specify whether conversion or auxiliary losses are excluded. It calls 30282 General load and 30284 Total load without defining their full accounting boundaries. Subtracting these registers cannot therefore be called inverter losses without observation and manufacturer confirmation.

This qualifies our House Load naming: integration of 30284 agrees with AC household demand in the examined installation/periods, but we have not established equivalence to 30282 under EV charging or other configurations. Do not change the source or rewrite history solely on the plugin's naming.

The earlier assessment that we had no candidate raw household-energy counter was incomplete: 30228 is a candidate worth testing. It cannot be recovered retrospectively from this database because it was not collected.

## The roughly 100 W battery-to-AC difference

[Upstream issue 417](https://github.com/TypQxQ/Sigenergy-Local-Modbus/issues/417) reports almost the same operating point. The maintainer checked direct ESS registers 30037/30599 against AC output 30031/30587, finding differences of 86–135 W across ten samples, averaging approximately 106 W. They attribute the likely explanation to conversion and auxiliary consumption but explicitly leave measurement boundaries, calibration and firmware interpretation unconfirmed. The integration applies no correction.

Our 2026-10-26 07:00–08:00 data has 60 minute samples: mean battery discharge 414.8 W, inverter AC output 329.45 W, gap 85.35 W, range 63–104 W. Before string production begins, 07:00–07:30 inclusive, the mean gap is 85.68 W. It is persistent, not a single event.

At sunrise the relevant comparison becomes battery discharge **plus string input**, less AC output. Between 08:00 and 09:00 this residual averages 102.71 W after excluding the isolated 08:50 transient; its remaining range is 84.7–117.7 W. This is an inferred electrical balance, not a directly measured losses register.

The upstream example also had substantial reactive power. Our hour has mean phase-A voltage 229.795 V, current 1.44983 A and mean V × |I| 333.205 VA, close to 329.45 W output. This gives no comparable evidence of a large reactive-current burden in our sample. It is only a screening check: direct reactive power and power factor were not recorded, and the inputs are not an atomic meter snapshot. The upstream explanation cannot simply be transferred to this installation.

Our phase-B/C voltage is 42,949,672.95 V and current is −0.01 A throughout those 60 samples, corresponding to all-one raw words after the configured decoding. These are apparent unavailable/unused-phase placeholders, not plausible electrical measurements. Do not sum them into apparent power. Exact device sentinel conventions need confirmation before a general decoder rule is introduced.

## Registers worth collecting in a diagnostic capture

All addresses below are read-only input registers in the integration's [register definitions](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/modbusregisterdefinitions.py). These are additions to investigate, not changes made by this review.

| Address | Quantity | Diagnostic purpose |
| --- | --- | --- |
| 30282 | General load power | Compare with existing 30284 and AC-side balance |
| 30228 | Common-load cumulative energy | Compare counter deltas against integrated household power and original Load |
| 30031 | Plant AC active power | AC balance at plant scope rather than a single inverter |
| 30599 | Inverter ESS charge/discharge power | Compare plant versus individual inverter battery readings |
| 31035 | Inverter PV power | Compare plant PV register with inverter PV and summed string V × I |
| 30589 / 31023 | Inverter reactive power / power factor | Test whether current burden correlates with the residual |
| 31003 / 30603 | PCS internal / battery average-cell temperature | Look for correlation with operating state; temperature is not a wattmeter |

General-load and total-load power share an adjacent four-word span and can be read together. Record grid connection status and relevant third-party PV/charger/generator flows when present; a balance omitting a real source or consumer is incomplete.

The newer statistics family includes battery charge/discharge at 30244/30248 and grid import/export at 30260/30264. Keep these separate from the existing 30200/30204 and 30216/30220 counters while comparing them. The protocol warns that 30228–30268 start counting afresh after a supporting firmware upgrade. Their absolute totals cannot be compared with old lifetime totals; compare deltas over common timestamps and retain reset handling.

## General lessons from register handling

### Capability discovery and failure isolation

The integration distinguishes supported, unsupported and unresolved registers. It caches capabilities against device identity and a register-definition fingerprint, invalidates them on changes, and probes remaining fields incrementally. Illegal-address responses are distinguished from temporary communication failures. See [support cache](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/support_cache.py#L48).

It reads strictly contiguous declared spans, capped at 123 words, and isolates failed blocks; unavailable fields remain missing. Our 120-word grouping can cross unconfigured gaps and one failed electrical block rejects the entire sample. Adding optional diagnostic registers to our existing required read plan could therefore reduce collection reliability on firmware that lacks them. A separate optional diagnostic path or explicit capability support is preferable to indiscriminately extending the map. See [device read planning and decoding](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/sigen_devices.py#L84).

Partial collection would require an explicit local data contract: retain healthy fields, mark failed fields missing, and only integrate a flow when its required inputs are valid. Do not substitute zero for a failed reading or silently mix old and new values.

### Timing and connection sharing

The integration records poll duration, partial failures and affected fields. It shares a serialized physical endpoint connection through Home Assistant's Modbus broker. Plant and inverter workflows still occur at different times; asynchronous code does not make the hardware snapshot atomic. See [transport adapter](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/modbus.py#L1) and [coordinator](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/coordinator.py#L76).

Our collector and live dashboard use independent connections. Our electrical timestamp is assigned after all blocks have been read. Logging read start/end and per-block completion would make transient balance errors easier to identify; a five-second diagnostic capture should be interpreted with these timing bounds. The plugin's default poll interval is five seconds, not evidence that faster collection eliminates skew.

### Counter resets, units and restart recovery

Selected upstream daily-energy sensors suppress unexpected zero readings after a positive value, while allowing local-midnight resets; this protection does not cover every energy counter. Our generic differencing skips a decrease but accepts the lower reading as the next baseline. A sequence such as 100 → 0 → 100.1 would then create a false 100.1 kWh interval. This is a verified code-path risk, not a claim that it caused today's gap. Review reset versus transient-zero policy before changing it. See [upstream zero guard](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/sensor.py#L259) and our [measurement storage](../../solar_model/storage/measurements.py).

The plugin normalizes power units before integration and carefully restores PV energy from snapshots/history; unavailable restore data stays unknown. Its [PV restore implementation](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/pv_restore.py) and [release notes](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/docs/releases/1.3.0.md) document fixes for repeated identical power reports and restoration. Our collector already integrates each successful timed sample, including identical values, so that event-driven undercount is not directly applicable. Keep native units independent of display units.

For string power, upstream returns zero if either voltage or current is non-positive. Our calculation clamps their product; two negative inputs can therefore produce a positive result. Validate physical input domains as well as the resulting power before treating unused or invalid channels as generation. This is a code-path difference, not an explanation established for our active strings.

### Future control work

Remote EMS commands receive fresh serialized readback; an old matching value is not accepted as confirmation, and a confirmation failure does not automatically resend the command. This is a useful design reference if device control is added later. A register readback confirms a setting, not the resulting physical battery behavior. Our application remains report-only. See [write confirmation](https://github.com/TypQxQ/Sigenergy-Local-Modbus/blob/16d710d7348b625521eba789110878c245e1903d/custom_components/sigen/coordinator.py#L240).

## Recommended next steps

1. Capture 30282, 30284 and 30228 alongside the existing signals, through optional reads that cannot disrupt production collection. Include battery/PV/AC diagnostics and read timing. Validate firmware support first.
2. Compare counter deltas and integrated power over the same boundaries: overnight steady discharge, higher loads, charging, sunrise and any EV activity. Preserve the original counters and raw observations.
3. Expose any derived gap as an estimated system/conversion residual with clear measurement boundaries. Keep it separate from household demand. Do not subtract a fixed 100 W or assume a fixed 5% from these observations.
4. Decide whether 30282 or 30228 improves household accounting only after these comparisons. Any source change needs explicit history, migration and naming decisions.
5. Address unavailable-phase validation and counter recovery as separate changes with focused regression checks.

No application code or database was changed, and no test suite was run for this source review. External plugin behavior was inspected, not verified against live hardware. The register comparison covered all 33 entries in our electrical map, not every control or metadata register in either project.

## Follow-up one-second capture

The temporary polling script, CSV captures and external source checkout were removed after the investigation at the user's request. The observations below are retained as a record; the capture files are no longer available locally. No production measurement source was changed: House Load continues to integrate register 30284, and no measured improvement from switching to 30282 was established.

Read-only analysis of the first 132 complete rows in `modbus_capture_20261026_143619.csv`, 2026-10-26 14:36:19.037–14:38:32.057 UTC (133.02 elapsed seconds). The file was still growing, so these figures describe that fixed prefix rather than a final full-file result. All 132 polls had zero errors. Mean scan duration was 571.58 ms, maximum 1,125 ms; two elapsed one-second slots were skipped.

| Quantity | Mean |
| --- | --- |
| Active-string power, calculated as positive voltage × positive current | 249.82 W |
| Inverter PV power, register 31035 | 161.80 W |
| Plant PV power, register 30035 | 161.70 W |
| Inverter battery discharge | 205.60 W |
| Inverter AC output | 366.34 W |
| General / total load power | 367.11 W / 367.11 W |

The string-input minus inverter-PV difference is present in every sample: mean 88.01 W, range 76.19–101.11 W. The full string-plus-battery-minus-AC residual averages 89.07 W. Reported inverter PV plus battery discharge agrees with AC output within 10 W in 122/132 samples. Thus the reported PV quantity already omits approximately the same power as the overall input/output residual in this operating state. This is consistent with net PV accounting after internal consumption/conversion, but neither a specific allocation nor a firmware correction formula is established; measurement offsets remain possible. Do not add both differences as separate losses.

For example, the first sample has 279.30 W from strings, 191 W reported PV, 176 W battery discharge and 365 W AC output. The input/output residual is 90.30 W, of which 88.30 W is already the string-to-reported-PV difference.

General load 30282 and total load 30284 match exactly in all 132 rows. Common-load counter 30228 and original lifetime-load counter 30094 also match exactly, including increments. Every captured old/new PV, battery and grid lifetime-counter pair matches. At this operating point the extra counters provide no distinct household-only measurement. Their 0.01 kWh resolution and this short window prevent a reliable counter-versus-integrated-power loss estimate: load counters advance 0.02 kWh while integrated load is approximately 0.01356 kWh; the difference is below one counter step.

Power factor is 1.000 in every sample and inverter reactive power ranges from −2 to +3 var. Battery temperature is fixed at 18.1 °C; PCS temperature ranges from 33.3 to 33.7 °C. The high-reactive-current explanation from upstream issue 417 is not supported by this capture.

Brief battery-reading differences remain: at 14:37:54.063 UTC, inverter discharge is 140 W while the later plant read reports 281 W. The inverter read occupies 0–78 ms of that poll and the plant read 234–359 ms. Opposite-sign discrepancies occur elsewhere, while their mean difference is only 0.40 W. This supports treating instantaneous cross-register extremes cautiously; timing/refresh differences or short control transients can affect them. It does not explain the persistent 88 W string-to-PV gap.

Unused PV3/PV4 inputs report −0.1 V and −0.01 A. They were excluded from the active-string sum. These illustrate why two negative placeholder inputs must not be multiplied into positive generation, although their numerical contribution here would be negligible.

### Extended capture through 14:46:19 UTC

Rechecked the first 591 complete rows, ending 2026-10-26 14:46:19.134 UTC, approximately ten minutes after capture began. The important new operating state is solar-only supply with essentially zero battery power and grid export. During the full 14:44 minute (60 samples), average string input is 624.31 W, AC output 548.48 W, household demand 335.60 W and grid export 212.67 W. Inverter battery power ranges from −1 to +2 W. String input minus reported inverter PV averages 75.90 W (range 63.52–82.86 W).

The gap therefore persists without meaningful battery discharge. It cannot be assigned entirely to battery-discharge conversion. General inverter/solar conversion, operating consumption or measurement interpretation remain candidates. Compared with the original mixed battery/solar interval's approximately 88 W PV gap, this state has an approximately 76 W gap; a single constant correction is not established.

Across all 591 samples, general and total load power still match exactly, as do common-load and original total-load counters. Household integrated energy is approximately 0.06237 kWh versus counter movement 0.07 kWh; the difference remains below the counter's 0.01 kWh step and cannot establish the loss boundary. Power factor remains 1.000 throughout.

One batch timed out at 14:44:59.115 UTC. Individual retries recovered all fields, but the poll took 1.875 seconds and subsequent polls use the narrower plan for that block. This changes sampling skew and explains why capture quality must be considered when comparing isolated spikes; the steady 14:44 PV gap is also present before this timeout.
