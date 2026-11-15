# SigenStor diagnostic probe — 2026-11-15

Read-only local Modbus capture, 09:53:12–09:53:26 UTC, on the installation reporting firmware `V100R001C00SPC117` at 09:40:24 UTC that day. An independent agent reviewed the temporary Python probe against current register/client code and the local V2.9 PDF before execution. No device setting or application database was changed by the probe. Requests used configured host/port/timeout, disabled retries, exact register reads and at least 1.05 seconds between request starts.

| Candidate | Device / register | Observed response | Decision |
| --- | --- | --- | --- |
| EMS work mode | 247 / 30003 | 1: AI mode | Add |
| Remote EMS control mode | 247 / 40031, holding read | 0: PCS remote-control selector | Omit inactive selector; EMS was not remote |
| Grid maximum export limitation | 247 / 40038, holding read | `[65535,65535]`, raw `0xFFFFFFFF` | Omit unusable limit; do not treat as 4,294,967.295 kW or infer unlimited |
| Inverter output type | 1 / 31004 | 0: L/N | Add |
| Inverter running state | 1 / 30578 | 1: Running | Add |
| Battery average cell temperature | 1 / 30603 | Raw 163 → 16.3 °C | Add |
| Inverter internal temperature | 1 / 31003 | Raw 286 → 28.6 °C | Add |
| Plant merged Alarm1–4 | 247 / 30027–30030 | Each zero | Add four distinct bitfields |
| Plant merged Alarm5 | 247 / 30072 | Zero | Add |
| Plant merged Alarm6 | 247 / 30280 | Zero | Add |
| Plant merged Alarm7 | 247 / 30281 | Exception function 0x84, code 2: illegal data address | Omit |

Protocol references: PDF physical pages 15–16, 18, 22, 24, 33 and 37–39 (printed pages 7–8, 10, 14, 16, 25 and 29–31); running-state labels are in Appendix 1, physical page 60. Existing code uses 0x04 for input and 0x03 for holding-register reads, consistent with table types and the PDF's function-code list. General RW text elsewhere in the PDF lists 0x04 instead; this probe used the established holding-read path and obtained responses.

This capture verifies responses at one operating point. It does not establish alarm activation, all operating-state transitions, temperature sentinels, dynamic derating, or the meaning of the all-one export-limit response. The temporary probe was not added as an application tool. Current behavior and fields belong in the [diagnostics contract](../implementation/sigenstor-diagnostics.md).
