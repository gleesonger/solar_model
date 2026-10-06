"""One-time chronological rebuild of adjusted forecasts with the live model.

Raw provider fields are never changed.  With ``--apply`` the script clears old
adjusted values for the requested target-date range, then replays each local
day hour by hour.  At each replay time only still-future target hours are
overwritten, matching the live model's information boundary.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import sys
from zoneinfo import ZoneInfo

import pandas as pd

from database import open_database
from forecast.operational_model import (
    PANEL_IDS,
    add_adjusted_energy_values,
    calculate_adjusted_forecast,
    load_operational_inputs,
)


def _parse_date(value: str) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _inferred_bounds(database, timezone: ZoneInfo) -> tuple[date, date]:
    """Use the available forecast history and actuals when bounds are omitted."""
    with database.engine.connect() as connection:
        earliest_forecast, latest_actual = connection.exec_driver_sql(
            "SELECT "
            "(SELECT MIN(forecast_time) FROM forecast_solar), "
            "(SELECT MAX(hour_start_utc) FROM sigenstor_hourly)"
        ).one()
    if earliest_forecast is None or latest_actual is None:
        raise ValueError("database needs both forecast and actual history to infer a rebuild range")
    return (
        datetime.fromtimestamp(earliest_forecast, timezone).date(),
        datetime.fromtimestamp(latest_actual, timezone).date(),
    )


def _print_progress(completed: int, total: int, local_day: date, updates: int) -> None:
    width = 30
    fraction = completed / total if total else 1.0
    filled = round(width * fraction)
    bar = "#" * filled + "-" * (width - filled)
    print(
        f"\rReplaying [{bar}] {completed}/{total} snapshots ({fraction:.0%}) "
        f"day {local_day} | {updates} target updates",
        end="",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild adjusted forecasts with the operational model")
    parser.add_argument("--database", default="solar.db")
    parser.add_argument("--start", type=_parse_date, help="First local day, inclusive (default: earliest forecast)")
    parser.add_argument("--end", type=_parse_date, help="Last local day, inclusive (default: latest actual)")
    parser.add_argument("--timezone", default="Europe/Dublin")
    parser.add_argument("--apply", action="store_true", help="Required to change the database")
    args = parser.parse_args()
    database = open_database(args.database)
    timezone = ZoneInfo(args.timezone)
    try:
        inferred_start, inferred_end = _inferred_bounds(database, timezone)
        start = args.start or inferred_start
        end = args.end or inferred_end
        if end < start:
            parser.error("--end must not precede --start")
        if not args.apply:
            print(
                "Dry run only. Re-run with --apply to replace adjusted forecast values "
                f"from {start} through {end}."
            )
            return

        # The replay only reads raw provider fields and actuals, so one load
        # is sufficient for the whole rebuild.  Adjusted output is accumulated
        # in memory and committed once per local day below.
        forecasts, actuals = load_operational_inputs(database)

        # Clear only target rows in this rebuild range.  Forecast rows for
        # other dates retain their existing adjusted values.
        fields = ", ".join(
            f"panel_{panel}_adj_{measure} = NULL"
            for panel in PANEL_IDS for measure in ("watts", "watt_hours", "watt_hours_day")
        )
        start_utc = datetime.combine(start, time.min, timezone).timestamp()
        end_utc = datetime.combine(end + timedelta(days=1), time.min, timezone).timestamp()
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE forecast_solar SET {fields} WHERE forecast_time >= ? AND forecast_time < ?",
                (start_utc, end_utc),
            )
        count = 0
        local_days = list(pd.date_range(start, end, freq="D").date)
        forecasts["snapshot_local_date"] = forecasts.snapshot.dt.tz_convert(timezone).dt.date
        snapshots_by_day = {
            local_day: sorted(forecasts.loc[
                forecasts.snapshot_local_date == local_day, "snapshot"
            ].unique())
            for local_day in local_days
        }
        total_snapshots = sum(len(snapshots) for snapshots in snapshots_by_day.values())
        completed_snapshots = 0
        for local_day in local_days:
            updates_by_snapshot: dict[str, dict[str, dict[str, float]]] = {}
            snapshots: dict[str, pd.Timestamp] = {}
            for as_of in snapshots_by_day[local_day]:
                calculated = calculate_adjusted_forecast(
                    forecasts, actuals, as_of, args.timezone,
                )
                if calculated is not None:
                    snapshot, values = calculated
                    snapshot_key = snapshot.isoformat()
                    snapshots[snapshot_key] = snapshot
                    # Later intraday calculations intentionally replace earlier
                    # values for the same forecast target within a snapshot.
                    updates_by_snapshot.setdefault(snapshot_key, {}).update(values)
                    count += len(values)
                completed_snapshots += 1
                _print_progress(completed_snapshots, total_snapshots, local_day, count)
            for snapshot_key, watt_updates in updates_by_snapshot.items():
                updates_by_snapshot[snapshot_key] = add_adjusted_energy_values(
                    forecasts, snapshots[snapshot_key], local_day, timezone, watt_updates,
                )
            # One transaction per rebuilt local day, retaining every provider
            # forecast vintage produced during that day.
            database.save_adjusted_forecasts_bulk(updates_by_snapshot)
        print(file=sys.stderr)
        print(f"Rebuilt {count} adjusted forecast target updates from {start} through {end}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
