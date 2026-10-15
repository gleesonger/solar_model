# Browser review cross-check supplement

2026-10-15. The independent initial assessment remains unchanged; this note records later adjudication and evidence retention.

The coordinator independently reproduced B1 (summed telemetry levels), B2 (asymmetric hour arrows) and B3 (mobile custom layout and banner-obstructed Close). I agree with these cross-checks.

I also agree with the coordinator's B4 classification: `implementation/dashboard-data.md` explicitly describes the implementation rule that all `_kwh` fields use last. Exported availability **11 kWh** therefore follows that documented detailed contract. The overview's “cumulative kWh” exception followed by “other numeric fields average” leaves state availability ambiguous. My five comparisons against **9.5 kWh** are **alternate-policy/documentation-contract discrepancies, not proven numerical output failures**. Explicitly classify/document state versus cumulative fields before any behavior change. This does not affect B1: adding SOC to 230% is a separate table-total problem.

Evidence is archived in [browser/README.md](system-review-evidence/browser/README.md), including portable launchers, synthetic databases, probes, actual downloads, browser options and selected screenshots. Launchers use repository `Path.cwd()` and isolated loopback ports. Original server PIDs 36980 and 35080 stopped after normal session interrupts; neither PID nor listening port remained. Windows reported exit status 1; shutdown-hook completion was not separately instrumented. Future archived launches have a local review-only `app.shutdown()` endpoint. No production service/database/configuration was changed.

## Supplemental browser workflows

After initial submission, `system-review-evidence/browser/supplemental_workflows.py` exercised additional actual desktop Chrome interactions. Exact combination: current review day, Last=1 day, Group by hour, Sum; custom variables **Solar + Load**, split **Day** (one Thu split in this fixture). Browser custom series sums were **4 kWh Solar** and **2 kWh Load**. Switching Solar off left only Load (2); switching Thu off removed all preview series. This closes those named interaction gaps but does not verify other metric/group/split cross-products.

The money **Split tariff** checkbox switched day/night series to one **Import Cost** series totaling **0.40** in browser chart data. Currency revenue/net bars retain the already documented one-decimal chart rounding; no new precision claim is made.

Control checks confirmed recalculation **Cancel** makes no action call. Empty From date, reversed dates and future To date each produced the expected validation error/status without invoking recalculation or restart. Existing action-log contents were compared before/after, so these checks do not depend on an empty historical log. The warning **Reload** action returned the isolated page to Recent / Live Today. No page errors were captured for these added workflows. Evidence: [supplemental-results.json](system-review-evidence/browser/supplemental-results.json) and `supplemental-custom.png`.

## Final graceful cleanup verification

The archived normal launcher was smoke-tested as PID **38128**, then stopped through its review-only shutdown endpoint with exit **0**. A final normal run PID **9692** and empty run PID **37944** supported the added workflows; both received the endpoint shutdown request and exited **0**. A final listener/process check found no PIDs 36980,35080,38128,9692,37944 and no listeners on 18091/18092. Source/configuration/production database stayed untouched. Temporary evidence is retained intentionally.
