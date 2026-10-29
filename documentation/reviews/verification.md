# Verification scope and limits

This page identifies available regression coverage and remaining verification work. It does not certify the latest working tree or report a fresh test run. Use [development and testing](../operations/development-and-testing.md) to check the revision being assessed; current contracts belong in subsystem guides and unresolved defects in [findings](findings.md).

## Regression coverage

- [Recovery checks](../../tests/regressions/test_review_recovery.py) and [cross-layer checks](../../tests/regressions/test_remaining_review_fixes.py) exercise forecast observation filtering, daily baseline retry, transactional backfill/recalculation, date boundaries and planning DST behavior with isolated inputs.
- [Migration checks](../../tests/storage/test_database_0_1_0_migration.py) and [grid statistics](../../tests/storage/test_grid_power_rollup.py) cover legacy preservation, readiness, sign-separated grid statistics and valid-reading counts.
- [Browser checks](../../tests/browser/) exercise synthetic dashboard rendering, freshness and interactions. [Planning tests](../../tests/planning/) check numerical/model contracts; [download tests](../../tests/dashboard/test_analysis_download.py) inspect exported values and workbook content.

Coverage describes what tests exercise, not their latest pass status. Mocked failures and synthetic inputs have narrower scope than production integration tests.

## Remaining integration work

- Validate real register support, sentinel conventions, installed-firmware response and provider failure/rate-limit behavior.
- Exercise actual process termination, interrupted migration and sustained collection/recalculation contention with isolated concurrent processes.
- Build and test the target Docker deployment and establish its network/access policy.
- Verify chronological live/replay forecast parity and ambiguous interval/data policies listed in findings.
- Extend independent browser output checks across null/forecast-only histories, date boundaries and interaction combinations. Test Android saved-page/freeze behavior on actual hardware.

Performance and register investigations retain their own dates, workloads and limits. They do not establish production latency guarantees, universal device semantics or successful battery control. Planning remains report-only.
