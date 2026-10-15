# Architecture review supplemental verification

2026-10-15, after initial independent report submission, the coordinator requested direct verification of the existing recalculation partial-commit finding. No other reviewer findings were read for this check.

**Confirmed, High reliability/correctness, high confidence:** `recalculate_economics.main()` commits raw updates before `database.rebuild_hourly_rollup()`. A subsequent rollup failure leaves raw and hourly economics inconsistent and tells the operator that nothing committed. This confirms the existing documented finding; it is not a newly discovered independent issue.

[Executable isolated reproduction](system-review-architecture-recalculation-evidence.py), run from repository root with the project Python 3.12 interpreter, creates one temporary sample for 2026-10-01 and its hourly aggregate. Its import energy is 1 kWh; original rate and cost are 0.1. It substitutes a complete new tariff at 0.5, mocks `SolarDatabase.rebuild_hourly_rollup()` to raise `SQLAlchemyError`, and invokes the real recalculation main/SQL transaction. No real configuration/database/process/device is used.

Observed output (SQLite values retain ×10,000 scaling):

```text
Exit: 1
Message: Recalculation failed; no changes committed: injected rollup failure
sigenstor [('new', 5000, 5000)]
sigenstor_hourly [('old', 1000, 1000)]
```

The committed raw row has rate/cost 0.5 while the hourly row remains 0.1, and normal `open_database()` does not reconcile it. Evidence database: `C:\Users\glees\AppData\Local\Temp\solar-architecture-recalculation-asumo3pt\fixture.db`. A controlled exception models failure before rollup rebuilding; actual interruption/lock timing was not exercised.

Recommendation: make raw update and affected-hour rebuild one atomic transaction where feasible. Otherwise persist explicit resumable rebuild state, report partial success accurately and provide recovery before serving summaries as current. Acceptance: inject failure between stages; either both raw/hourly remain at 0.1 or reopening/retrying completes both to 0.5 exactly once (tolerance 0.0001 in physical units); failure text must describe actual commit state. Preserve previously successful history, date boundaries and concurrent collector writes.
