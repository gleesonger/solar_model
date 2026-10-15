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

## Backups and troubleshooting

The first startup after the R06 valid-count upgrade rebuilds hourly history once for databases lacking `hourly_rollup_initialized_v3` (including v1/v2 databases), inside a write transaction. This populates per-field valid counts and split grid statistics without changing raw data. Large histories may take longer and temporarily block other writers. Use the managed launcher so this completes before its children start; later opens only check readiness. Failed/interrupted initialization rolls back and retries.

- Make a consistent SQLite backup (SQLite backup API or copy while writers are stopped); retain the matching configuration and register maps.
- Invalid config: inspect startup `ValueError`, field spelling, complete rule coverage, effective dates, and YAML types.
- Missing readings: check device/network/map IDs, server logs, and warning timestamp age. Failed reads retain old live values.
- Stale summaries after tariff changes: confirm raw update and hourly rebuild both completed.
- Blank forecasts: check pre-midnight history, configured-panel availability, partial requests and adjustment failure.
- Planning failure: check required metadata/history, interval alignment, HiGHS, and infeasible battery/power limits.
