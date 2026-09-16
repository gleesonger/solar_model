"""Apply effective-dated tariff rates and costs to stored Modbus samples."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from config import Config, load_config
from economics import tariff_rule
from database import open_database


def parse_date(value: str) -> date:
    try:
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError("date must use YYYY-MM-DD")
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


@dataclass(frozen=True)
class IntervalRow:
    effective_from: str
    next_effective: str | None
    weekday: int
    start_time: str
    end_time: str
    import_rate: int
    import_tariff_band: str | None
    export_rate: int

    def sql_values(self) -> dict[str, str | int | None]:
        return self.__dict__.copy()


def interval_rows(config: Config) -> list[IntervalRow]:
    periods = config.tariffs.periods
    rows: list[IntervalRow] = []
    for index, period in enumerate(periods):
        next_date = periods[index + 1].effective_from if index + 1 < len(periods) else None
        for weekday in range(7):
            boundaries = {0, 1440}
            for rule in (*period.import_, *period.export):
                boundaries.add(rule.start_minutes)
                boundaries.add(rule.end_minutes)
            boundaries = sorted(boundaries)
            for start, end in zip(boundaries, boundaries[1:]):
                timestamp = datetime(2024, 1, 1 + weekday, start // 60, start % 60)
                import_rule = tariff_rule(period.import_, timestamp)
                rows.append(IntervalRow(period.effective_from, next_date, (weekday + 1) % 7, f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}", round(import_rule.rate * 10000), import_rule.name, round(tariff_rule(period.export, timestamp).rate * 10000)))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--from", dest="start", default=datetime(2000,1,1).date(), type=parse_date)
    parser.add_argument("--to", dest="end", type=parse_date)
    args = parser.parse_args(argv)
    if args.end is not None and args.end < args.start:
        parser.error("--to must be on or after --from")

    try:
        config = load_config(args.config)
        path = Path(config.database.path)
        if not path.is_file():
            raise ValueError(f"database does not exist: {path.resolve()}")
        zone = ZoneInfo(config.timezone)
        minimum = int(datetime.combine(args.start, datetime.min.time(), zone).timestamp())
        maximum = None if args.end is None or args.end == date.max else int(datetime.combine(date.fromordinal(args.end.toordinal() + 1), datetime.min.time(), zone).timestamp())
        database = open_database(path)
        try:
            with database.engine.connect() as connection:
                raw = connection.connection.driver_connection
                def configured_localtime(value: int) -> str:
                    return datetime.fromtimestamp(value, zone).strftime("%Y-%m-%d %H:%M:%S")

                assert raw is not None, "raw connection is None"

                raw.create_function("configured_localtime", 1, configured_localtime)
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("CREATE TEMP TABLE tariff_intervals (effective_from TEXT, next_effective TEXT, weekday INTEGER, start_time TEXT, end_time TEXT, import_rate INTEGER, import_tariff_band TEXT, export_rate INTEGER)")
                raw.executemany("INSERT INTO tariff_intervals VALUES (:effective_from, :next_effective, :weekday, :start_time, :end_time, :import_rate, :import_tariff_band, :export_rate)", [row.sql_values() for row in interval_rows(config)])
                params = {"minimum": minimum, "maximum": maximum}
                raw.execute("""
                WITH matched AS (
                    SELECT
                        sample.collected_at_utc,
                        tariff.import_rate,
                        tariff.import_tariff_band,
                        tariff.export_rate
                    FROM sigenstor AS sample
                    JOIN tariff_intervals AS tariff
                      ON date(configured_localtime(sample.collected_at_utc)) >= tariff.effective_from
                     AND (tariff.next_effective IS NULL OR date(configured_localtime(sample.collected_at_utc)) < tariff.next_effective)
                     AND CAST(strftime('%w', configured_localtime(sample.collected_at_utc)) AS INTEGER) = tariff.weekday
                     AND time(configured_localtime(sample.collected_at_utc)) >= tariff.start_time
                     AND time(configured_localtime(sample.collected_at_utc)) < tariff.end_time
                    WHERE sample.collected_at_utc >= :minimum
                      AND (:maximum IS NULL OR sample.collected_at_utc < :maximum)
                )
                UPDATE sigenstor AS sample
                   SET import_rate = matched.import_rate,
                       import_tariff_band = matched.import_tariff_band,
                       export_rate = matched.export_rate,
                       grid_import_cost_period = ROUND(sample.plant_grid_import_total_kwh_period * matched.import_rate / 10000.0),
                       grid_export_revenue_period = ROUND(sample.plant_grid_export_total_kwh_period * matched.export_rate / 10000.0),
                       no_solar_battery_import_cost_period = ROUND(sample.plant_load_total_kwh_period * matched.import_rate / 10000.0),
                       net_cost_period = ROUND((sample.plant_grid_import_total_kwh_period * matched.import_rate - sample.plant_grid_export_total_kwh_period * matched.export_rate) / 10000.0)
                  FROM matched
                 WHERE sample.collected_at_utc = matched.collected_at_utc
                """, params)
                raw.execute("DROP TABLE tariff_intervals")
                raw.commit()
            # Dashboard summaries use sigenstor_hourly, so replacing the
            # selected rollup range is required after recalculating its minute
            # source rows.
            database.rebuild_hourly_rollup(args.start, args.end or datetime.now(zone).date(), zone)
        finally:
            database.close()
    except (ValueError, OSError, SQLAlchemyError) as error:
        parser.exit(1, f"Recalculation failed; no changes committed: {error}\n")
    print(f"Applied tariff rates from {args.start} through {args.end or 'all available dates'}.")
    print("Dates without a configured effective tariff remain unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
