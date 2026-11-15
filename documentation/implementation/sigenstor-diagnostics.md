# SigenStor diagnostic collection

Sources: [register maps](../../solar_model/integrations/sigenstor/register_maps/), [register grouping](../../solar_model/integrations/sigenstor/registers.py), [collector service](../../solar_model/services/collection.py), [status labels](../../solar_model/integrations/sigenstor/metadata.py). Definitions follow the [V2.9 protocol](../reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf).

## Retained fields

All fields below are input-register reads, explicitly optional, and non-cumulative. Plant device address is 247; this installation's inverter address is 1.

| Metric / stable name | Device | Address | Encoding | Persistence |
| --- | --- | --- | --- | --- |
| EMS work mode / `ems_work_mode` | 247 | 30003 | U16 code | Device change history |
| Inverter output type / `inverter_output_type` | 1 | 31004 | U16 code | Device change history |
| Inverter running state / `inverter_running_state` | 1 | 30578 | U16 code | Device change history |
| Plant alarm 1–4 / `plant_alarm_1`–`plant_alarm_4` | 247 | 30027–30030 | One U16 bitfield each | Device change history |
| Plant alarm 5 / `plant_alarm_5` | 247 | 30072 | U16 bitfield | Device change history |
| Plant alarm 6 / `plant_alarm_6` | 247 | 30280 | U16 bitfield | Device change history |
| `inverter_battery_avg_cell_temperature_c` | 1 | 30603 | S16 / 10, °C | Timestamped telemetry |
| `inverter_internal_temperature_c` | 1 | 31003 | S16 / 10, °C | Timestamped telemetry |

Plant alarm words 1–6 correspond to the PDF's merged Alarm1–6: PCS alarm sets 1/2, ESS, gateway, DC charger, and plant alarm set 1. Equal zero readings indicate clear words at that observation, not redundant alarm definitions. Individual inverter alarm words are omitted because their merged plant counterparts cover this installation's reporting needs.

The three states are different: EMS work mode describes energy management, output type describes phase wiring, and running state describes inverter operation. System Info adds code labels without replacing raw numeric history. Unknown codes remain visible. Zero is valid for all nine metadata fields, including transitions back to standby/self-consumption/no alarms; repeated unchanged values do not append rows.

Temperatures use the standard scaled-integer raw/hourly path: physical °C at the ORM boundary, stored integers ×10,000, nullable missing readings, hourly sums with separate valid counts, average valid values in full-data downloads. Zero and negative temperatures remain observations. Existing database opening adds nullable columns and hourly counts without synthesizing past readings or rebuilding marked history. These fields have explicit °C export labels; this change does not add temperature charts or live-dashboard polling.

## Read and failure boundaries

Each optional definition has its own block; it cannot expand a required span. The collector reads required blocks first, then attempts optional blocks independently. A failed diagnostic is logged/omitted; successful required electrical readings and other diagnostics can still persist. Required failures retain their existing interval-failure behavior. Device history remains a separate transaction after electrical persistence.

Existing required spans can already include these addresses in gaps; this change does not alter those spans or guarantee that an address appears only in optional reads. Production collection retains its existing request timing; the independent one-shot probe used 1.05 seconds between request starts. Optional reads add eleven requests per complete collector cycle. The application sends no Modbus writes.

An optional failure leaves temperature null for that electrical sample and retains the previous successful metadata value in System Info. A missing alarm response must not be interpreted as a cleared alarm; the table is latest-known metadata, not a fresh alarm monitor.

## Refinement evidence

The [2026-11-15 probe](../reviews/sigenstor-diagnostics-2026-11-15.md) tested the distinct candidates on firmware `V100R001C00SPC117`. Extra battery rated/configured/current power fields, backup SOC and cell-voltage extrema were omitted from this change as strong overlaps with existing readings. They are not claimed to be semantically equivalent in every operating condition.

Grid export limitation returned all ones and plant Alarm7 returned illegal data address; neither is added. Remote EMS control mode returned zero while EMS work mode was AI, so the inactive configured selector is omitted. Export-limit sentinel meaning and untested operating-state behavior remain [review findings](../reviews/findings.md).

Targeted regression coverage is in [test_sigenstor_diagnostics.py](../../tests/storage/test_sigenstor_diagnostics.py): required/optional failure boundaries, zero states/alarm clearing, signed temperatures, missing/zero values, raw scaling, hourly rebuild agreement, export averaging and additive migration on temporary databases.
