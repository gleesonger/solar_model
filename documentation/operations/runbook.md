# Operations

For maintenance call chains, temporary schedules, replay buffers, research features, and output artifacts, see [maintenance and research implementation](../implementation/maintenance-research.md).

## Launch and restart

```powershell
python docker_start.py
```

- Run from the directory containing the intended `config.yaml`. Relative database paths use that directory; register maps/assets resolve relative to source.
- Supervisor migrates/initialises the database, starts collector/dashboard, and stops both if a child exits. It is not an automatic crash-retry service.
- Control restart touches `SOLAR_MODEL_RESTART_FILE`; supervisor stops children and reloads configuration. Standalone dashboard lacks this full-restart facility.
- For development only: `python main_dashboard.py` or `python main_data_collection.py`. These still use configured hardware/database.
- Shutdown terminates children, waits up to 10 seconds, then kills survivors.

## Docker

```powershell
docker build -t solar-model .
docker run -p 8090:8090 -v solar-config:/config solar-model
```

- [Dockerfile](../../Dockerfile) installs dependencies, places source in `/app`, and runs the managed launcher from `/config`.
- [docker-entrypoint.sh](../../docker-entrypoint.sh) copies the shipped YAML only if `/config/config.yaml` does not exist. Image upgrades do not overwrite existing configuration.
- `/config` holds persistent configuration/database; publish the actual configured dashboard port. Dockerfile's `EXPOSE 8080` differs from shipped 8090.
- The dashboard has no application authentication layer in the reviewed source; Control actions are available to clients that can reach it. Restrict access using deployment/network controls where needed.

## Maintenance commands and effects

Use a verified backup and preferably stop persisted collection before bulk rewrites. `open_database()` itself may migrate/backfill, even in a forecast-rebuild dry run.

```powershell
# Read stored forecast performance; optional --date and --csv
python -m forecast.evaluate_forecasts --database solar.db

# Report-only load experiment; choose dates with existing history
python -m forecast.evaluate_load_profile --database solar.db --start 2026-09-01 --end 2026-09-30

# Forecast rebuild preview; add --apply to clear/replace adjusted fields
python -m forecast.rebuild_operational_adjusted_forecasts --database solar.db --start 2026-09-01 --end 2026-09-30

# Immediately overwrites rates/costs, then rebuilds affected hourly rows
python recalculate_economics.py --config config.yaml --from 2026-09-01 --to 2026-09-30

# Reads existing schema and writes/replaces the specified workbook
python test_energy_plan_as_of.py --database solar.db --as-of "2026-09-15T00:00:00+01:00" --output review-plan.xlsx
```

- Recalculation date bounds are inclusive; omitted `--to` updates all later matching raw rows. CLI alone does not restart the application; Control does.
- Raw tariff updates and rollup rebuild share one transaction; either both commit or both roll back. Without `--to`, rebuilding extends through the latest available raw date. Neighboring samples in overlapping UTC hours are retained.
- Forecast rebuild infers missing date bounds from earliest forecast/latest hourly actual. It commits clearing first and results by day, so interruption can leave a partial rebuild. It preserves raw fields.
- `python -m forecast.solar_adjustment --database solar.db` writes pending adjusted fields using the older tree model. It is not the operational rebuild; avoid mixing outputs unintentionally.
- Training/backtest tools default to `forecast/solar.db`; pass `--database solar.db` explicitly when using the root database.

## Backup, restore and recovery

Stop the managed application/container before restoring or running bulk maintenance. Retain the matching configuration, register maps and source revision with the backup. Choose paths explicitly; examples below use the repository's standard `solar.db` path, which may differ from `config.database.path`.

### Create and verify a backup

From the repository root, this command opens the source **read-only**, uses SQLite's backup API, refuses an existing destination, and checks the result. It does not call the application's migrating opener. The backup API also works with a live database; stop writers when you need a fixed maintenance baseline and matching configuration snapshot.

```powershell
@'
import sqlite3
from contextlib import closing
from pathlib import Path

source = Path("solar.db").resolve(strict=True)
destination = Path("solar-before-maintenance.db").resolve()
if destination.exists():
    raise SystemExit("Choose a new backup filename; destination already exists")
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
    with closing(sqlite3.connect(destination)) as backup:
        original.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise SystemExit(f"Backup failed integrity check: {result}")
print(f"Verified backup: {destination}")
'@ | .\.venv\Scripts\python.exe -
```

Save the configuration/maps separately; a database backup does not include them. Do not copy only the main database file while writers are active: journal/WAL state can matter even though this application does not explicitly enable WAL.

### Restore and validate

1. Stop **all** processes using the destination database, including standalone dashboard/collector and maintenance scripts. For Docker, stop the container first and work on its persistent `/config` data.
2. Preserve the failed/current database and any `-wal`, `-shm`, or `-journal` siblings together in a separate recovery directory. Do not overwrite the only copy or mix old sidecars with the replacement.
3. Copy the verified backup to a new `restore-check.db`. Inspect it through read-only SQLite first: `PRAGMA integrity_check`, raw/forecast counts and time ranges. Compare with values recorded before maintenance; missing history is not repaired by a successful integrity check.
4. Test the intended source revision's `open_database("restore-check.db")` on this expendable copy, then close it and repeat the checks. Opening can migrate/rebuild; the original backup must remain untouched. Reopening should preserve raw values and complete hourly readiness.
5. With writers still stopped and the destination clear of the prior file/sidecars, copy the verified backup into the configured database path. Restore the matching configuration/maps, check file ownership for Docker, and launch through `docker_start.py`.
6. Confirm persisted timestamps advance, recent known energy/cost totals are plausible, and hourly initialization succeeds without repeated rebuilds. A synthetic live reading or healthy browser connection alone does not prove persisted collection is working.

Useful **read-only SQL** for before/after comparison (timestamps here are UTC epoch seconds):

```sql
PRAGMA integrity_check;
SELECT COUNT(*), MIN(collected_at_utc), MAX(collected_at_utc) FROM sigenstor;
SELECT COUNT(*), MIN(collected_at_utc), MAX(collected_at_utc),
       MIN(forecast_time), MAX(forecast_time) FROM forecast_solar;
SELECT COUNT(*), SUM(sample_count), MIN(hour_start_utc), MAX(hour_start_utc)
FROM sigenstor_hourly;
SELECT key, value FROM database_metadata;
```

Run only queries for tables present in the backup; a legacy schema may lack hourly/metadata tables. After current initialization, hourly sample counts should account for raw rows, including rows with missing measurements. Raw/forecast counts and ranges should not change just because hourly history was rebuilt. SQL checks require a read-only SQLite connection; do not use `open_database()` merely to inspect an unknown backup.

### Interrupted maintenance

| Operation | Recovery boundary and next step |
| --- | --- |
| Initial hourly rebuild/statistics upgrade | Readiness and replacement commit together. Restart with the same intended revision and let initialization retry; do not fabricate a completion marker. Raw history is retained. |
| Other schema changes in `open_database()` | The entire opener is not one migration transaction. Preserve the failed database, inspect the exception, and test repeat opening on a copy. Some renames/additions may already have committed; conflicting legacy/raw forecast values require reconciliation, not forced column removal. |
| Tariff recalculation | Raw rates/costs and hourly replacement share one transaction. After fixing the failure, rerun the same dates/configuration; inspect both raw and hourly values. Initialization performed before recalculation is a separate transaction. |
| Operational forecast rebuild | Clearing commits before per-day writes. For the same model/configuration, rerun the original explicit range to recompute it; this is not checkpoint resume. To restore the exact prior adjusted history, use the backup. Replay still has its documented future-day parity limitation. |
| Source rollback | Do not assume older source understands a migrated schema. Test the old revision against a copy, or restore its matching pre-upgrade backup/configuration. No reverse-migration command exists. |

## Troubleshooting

The first startup after the R06 valid-count upgrade rebuilds hourly history once for databases lacking `hourly_rollup_initialized_v3` (including v1/v2 databases), inside a write transaction. This populates per-field valid counts and split grid statistics without changing raw data. Large histories may take longer and temporarily block other writers. Use the managed launcher so this completes before its children start; later opens only check readiness. Failed/interrupted initialization rolls back and retries.

- Invalid config: inspect startup `ValueError`, field spelling, complete rule coverage, effective dates, and YAML types.
- Missing readings: check device/network/map IDs, server logs, and warning timestamp age. Failed reads retain old live values.
- Stale summaries after tariff changes: confirm raw update and hourly rebuild both completed.
- Blank forecasts: check pre-midnight history, configured-panel availability, partial requests and adjustment failure.
- Planning failure: check required metadata/history, interval alignment, HiGHS, and infeasible battery/power limits.
