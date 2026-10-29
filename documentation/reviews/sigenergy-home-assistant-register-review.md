# Sigenergy register review

Historical source/protocol review and read-only observations from 2026-10-26, condensed on 2026-10-29. Upstream baseline: [Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus/tree/16d710d7348b625521eba789110878c245e1903d), release 1.3.0. All 33 corresponding electrical definitions matched address, width, signedness and scale; no decoding explanation for the persistent electrical residual was found. No production source was changed or control command sent.

## Load boundaries

| Register | Meaning | Local use / conclusion |
| --- | --- | --- |
| 30282 | General load power | Upstream preferred household input; not stored locally. |
| 30284 | Total load power | `plant_load_power_kw`, integrated as House Load. |
| 30092 / 30094 | Daily / lifetime total-load energy | Original Load counters, separate from House Load. |
| 30228 | Common-load lifetime energy | Not stored locally. Excludes EV charging without proving loss exclusion. |

The [vendor protocol](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf) does not fully define these accounting boundaries. One overnight hour averaged 0.337 kW household power while Load counters advanced 0.70 kWh; AC output minus export agreed with household power, with roughly 0.35 kW battery-to-AC residual. This suggests different loss accounting but does not establish firmware semantics.

## Capture conclusions

- A 591-sample diagnostic capture found General/Total load power identical and common/original load counters identical. No improvement from switching registers was demonstrated; the short-window counter/integrated-energy difference was below one 0.01 kWh counter step.
- The initial mixed solar/battery window had an approximately 88 W string-to-reported-PV gap, accounting for almost all the approximately 89 W string-plus-battery-minus-AC residual. Do not add those as separate losses.
- A solar-only minute retained an approximately 76 W string-to-PV gap with negligible battery power. The residual cannot be assigned entirely to battery-discharge conversion; no fixed-watt or percentage correction is justified.
- Power factor remained 1.000 and reactive power near zero, so high reactive current was not supported as the explanation here. [Upstream issue 417](https://github.com/TypQxQ/Sigenergy-Local-Modbus/issues/417) reports a similar gap and applies no correction.
- Sequential reads and one recovered timeout caused timing skew; isolated cross-register spikes are not atomic balance measurements. Capture scripts/files were removed after the investigation.

## Follow-up priorities

1. Validate unavailable phase/PV sentinels and physical input domains. Do not use implausible all-one phase readings or multiply two negative placeholders into generation.
2. Distinguish transient zeros from genuine counter resets; the current 100 -> 0 -> 100.1 baseline path can overcount recovery.
3. Isolate optional/unsupported diagnostic registers so a failed block cannot reject healthy required telemetry. Preserve missing values and record read timing; collector and live dashboard use separate connections.
4. Compare 30282/30284/30228 with battery/PV/AC diagnostics over common boundaries, including charging, sunrise and EV activity, before changing sources, naming or history. New 30228-30268 statistics may reset after firmware upgrades; compare deltas rather than absolute totals.
5. Future control needs fresh serialized readback, ownership and handover policy. Readback alone does not verify physical behavior; this application remains report-only.

Exact loss allocation, calibration and firmware behavior remain unverified. See [open findings](findings.md).
