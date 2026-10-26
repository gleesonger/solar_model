"""Read-only comparison of recorded load energy and integrated house load."""

import argparse
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=Path("solar.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timezone", default="Europe/Dublin")
    parser.add_argument("--end", type=date.fromisoformat, help="Exclusive local end date")
    parser.add_argument("--max-gap-seconds", type=int, default=120)
    args = parser.parse_args()
    if args.days <= 0 or args.max_gap_seconds <= 0:
        parser.error("days and max-gap-seconds must be positive")

    zone = ZoneInfo(args.timezone)

    def midnight(day: date) -> float:
        return datetime.combine(day, time.min, zone).timestamp()

    # Do not use the application opener: it can migrate/backfill the database.
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        latest = db.execute("SELECT MAX(collected_at_utc) FROM sigenstor").fetchone()[0]
        if latest is None:
            parser.error("database has no measurements")
        end = args.end or datetime.fromtimestamp(latest, zone).date()
        start = end - timedelta(days=args.days)
        lower, upper = midnight(start), midnight(end)
        columns = "collected_at_utc, plant_load_power_kw, plant_load_total_kwh_period"
        previous = db.execute(
            f"SELECT {columns} FROM sigenstor WHERE collected_at_utc < ? "
            "ORDER BY collected_at_utc DESC LIMIT 1", (lower,),
        ).fetchone()
        rows = db.execute(
            f"SELECT {columns} FROM sigenstor WHERE collected_at_utc >= ? "
            "AND collected_at_utc <= ? ORDER BY collected_at_utc", (lower, upper),
        )
        days = [start + timedelta(days=i) for i in range(args.days)]
        # Counter energy, house energy, covered seconds, valid counter samples.
        totals = {day: [0.0, 0.0, 0.0, 0] for day in days}
        for stamp, power, energy in rows:
            if lower <= stamp < upper and energy is not None:
                daily = totals[datetime.fromtimestamp(stamp, zone).date()]
                daily[0] += energy / 10000
                daily[3] += 1
            if previous is not None:
                old_stamp, old_power, _ = previous
                elapsed = stamp - old_stamp
                if (old_power is not None and power is not None
                        and 0 < elapsed <= args.max_gap_seconds):
                    left, stop = max(old_stamp, lower), min(stamp, upper)
                    while left < stop:
                        day = datetime.fromtimestamp(left, zone).date()
                        right = min(stop, midnight(day + timedelta(days=1)))
                        # Interpolate boundary powers before splitting across local days.
                        left_power = old_power + (power - old_power) * (left - old_stamp) / elapsed
                        right_power = old_power + (power - old_power) * (right - old_stamp) / elapsed
                        daily = totals[day]
                        daily[1] += (left_power + right_power) / 2 / 10000 * (right - left) / 3600
                        daily[2] += right - left
                        left = right
            previous = (stamp, power, energy)

    print(f"{start} to {end} (exclusive), {args.timezone}; {args.days} complete calendar days")
    print(f"House load: trapezoidal power integration; gaps > {args.max_gap_seconds}s skipped.")
    print("Load: stored counter deltas, attributed to their collection date.")
    print("Difference is not a verified loss meter; compare coverage before interpreting it.\n")
    print(f"{'Date':<12} {'Load kWh':>10} {'House kWh':>11} {'Diff kWh':>10} {'House coverage':>15}")
    for day in days:
        load, house, covered, count = totals[day]
        duration = midnight(day + timedelta(days=1)) - midnight(day)
        load_text = f"{load:.3f}" if count else "-"
        house_text = f"{house:.3f}" if covered else "-"
        diff_text = f"{load - house:.3f}" if count and covered else "-"
        print(f"{day!s:<12} {load_text:>10} {house_text:>11} {diff_text:>10} {covered / duration:>14.1%}")
    load = sum(t[0] for t in totals.values())
    house = sum(t[1] for t in totals.values())
    coverage = sum(t[2] for t in totals.values()) / (upper - lower)
    print(f"{'TOTAL':<12} {load:>10.3f} {house:>11.3f} {load - house:>10.3f} {coverage:>14.1%}")


if __name__ == "__main__":
    main()
