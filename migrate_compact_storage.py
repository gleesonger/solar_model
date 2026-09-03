"""Migrate sample and forecast storage to UTC epochs and scaled integers.

Run ``python migrate_compact_storage.py --snapshot-only`` before migration to
record reference values. Then run ``python migrate_compact_storage.py`` while
the dashboard and collectors are stopped.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCALE = 10_000
LEGACY_SAMPLE_TABLE = "sigenstor_samples"
LEGACY_FORECAST_TABLE = "forecast_solar_samples"
SAMPLE_TABLE = "sigenstor"
FORECAST_TABLE = "forecast_solar"
SNAPSHOT_NAME = "compact_storage_snapshot.json"

SAMPLE_SNAPSHOT_COLUMNS = (
    "plant_pv_power_kw",
    "plant_load_power_kw",
    "plant_grid_import_total_kwh",
    "plant_pv_total_kwh",
    "inverter_battery_soc_percent",
    "inverter_battery_available_discharge_kwh",
    "import_rate",
    "grid_import_cost_period",
)
FORECAST_SNAPSHOT_COLUMNS = (
    "panel_1_watts",
    "panel_1_watt_hours",
    "panel_2_watts",
    "panel_2_watt_hours",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", default="solar.db", type=Path)
    parser.add_argument("--snapshot-only", action="store_true")
    parser.add_argument("--timezone", default="Europe/Dublin")
    args = parser.parse_args()

    snapshot_path = args.database.with_name(SNAPSHOT_NAME)
    if args.snapshot_only:
        write_snapshot(args.database, snapshot_path)
        return

    if not snapshot_path.exists():
        raise SystemExit(f"Create the pre-migration snapshot first: {snapshot_path}")
    backup_path = args.database.with_suffix(
        args.database.suffix + f".pre_compact.{datetime.now():%Y%m%d%H%M%S}.bak"
    )
    shutil.copy2(args.database, backup_path)
    migrate(args.database, ZoneInfo(args.timezone))
    verify_snapshot(args.database, snapshot_path)
    compact_database(args.database)
    print(f"Migration complete. Backup: {backup_path}")


def write_snapshot(database_path: Path, snapshot_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        snapshot = {
            "scale": SCALE,
            "samples": sampled_rows(
                connection, LEGACY_SAMPLE_TABLE, SAMPLE_SNAPSHOT_COLUMNS, ("id", "collected_at_utc")
            ),
            "forecasts": sampled_rows(
                connection,
                LEGACY_FORECAST_TABLE,
                FORECAST_SNAPSHOT_COLUMNS,
                ("id", "collected_at_utc", "forecast_time"),
            ),
        }
    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"Wrote reference snapshot: {snapshot_path}")


def sampled_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    identity_columns: tuple[str, ...],
) -> list[dict[str, object]]:
    count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    offsets = sorted({0, count // 2, count - 1})
    selected_columns = (*identity_columns, *columns)
    selected = ", ".join(selected_columns)
    rows: list[dict[str, object]] = []
    for offset in offsets:
        row = connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY id LIMIT 1 OFFSET ?", (offset,)
        ).fetchone()
        rows.append(dict(zip(selected_columns, row, strict=True)))
    return rows


def migrate(database_path: Path, timezone: ZoneInfo) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        sample_columns = numeric_columns(connection, LEGACY_SAMPLE_TABLE, excluded={"id", "collected_at_utc", "collected_at_local"})
        forecast_columns = numeric_columns(
            connection,
            LEGACY_FORECAST_TABLE,
            excluded={"id", "collection_guid", "collected_at_utc", "collected_at_local", "forecast_time"},
        )
        ensure_unique_collection_times(connection)
        connection.execute("BEGIN IMMEDIATE")
        create_sample_table(connection, sample_columns)
        create_forecast_table(connection, forecast_columns)
        copy_samples(connection, sample_columns)
        copy_forecasts(connection, forecast_columns, timezone)
        connection.execute(f"DROP TABLE {LEGACY_SAMPLE_TABLE}")
        connection.execute(f"DROP TABLE {LEGACY_FORECAST_TABLE}")
        connection.execute(f"ALTER TABLE {SAMPLE_TABLE}_compact RENAME TO {SAMPLE_TABLE}")
        connection.execute(f"ALTER TABLE {FORECAST_TABLE}_compact RENAME TO {FORECAST_TABLE}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def numeric_columns(connection: sqlite3.Connection, table: str, excluded: set[str]) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[1] not in excluded]


def ensure_unique_collection_times(connection: sqlite3.Connection) -> None:
    duplicate = connection.execute(
        f"SELECT collected_at_utc FROM {LEGACY_SAMPLE_TABLE} GROUP BY collected_at_utc HAVING count(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise ValueError(f"Duplicate sample collection timestamp: {duplicate[0]}")

    duplicate_forecast = connection.execute(
        f"SELECT collected_at_utc, forecast_time FROM {LEGACY_FORECAST_TABLE} "
        "GROUP BY collected_at_utc, forecast_time HAVING count(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate_forecast is not None:
        raise ValueError(f"Duplicate forecast collection/time: {tuple(duplicate_forecast)}")

def create_sample_table(connection: sqlite3.Connection, columns: list[str]) -> None:
    fields = ["collected_at_utc INTEGER PRIMARY KEY", *(f'"{column}" INTEGER' for column in columns)]
    connection.execute(f"CREATE TABLE {SAMPLE_TABLE}_compact ({', '.join(fields)}) STRICT")


def create_forecast_table(connection: sqlite3.Connection, columns: list[str]) -> None:
    fields = [
        "collected_at_utc INTEGER NOT NULL",
        "forecast_time INTEGER NOT NULL",
        *(f'"{column}" INTEGER' for column in columns),
        "PRIMARY KEY (collected_at_utc, forecast_time)",
    ]
    connection.execute(f"CREATE TABLE {FORECAST_TABLE}_compact ({', '.join(fields)}) STRICT, WITHOUT ROWID")
    connection.execute(
        f"CREATE INDEX idx_forecast_time_compact ON {FORECAST_TABLE}_compact (forecast_time)"
    )


def copy_samples(connection: sqlite3.Connection, columns: list[str]) -> None:
    source_columns = ["collected_at_utc", *columns]
    placeholders = ", ".join("?" for _ in source_columns)
    target_columns = ", ".join(f'"{column}"' for column in source_columns)
    rows = connection.execute(f"SELECT {target_columns} FROM {LEGACY_SAMPLE_TABLE}")
    converted = (
        (to_epoch(row["collected_at_utc"], None), *(scale_value(row[column]) for column in columns))
        for row in rows
    )
    connection.executemany(f"INSERT INTO {SAMPLE_TABLE}_compact ({target_columns}) VALUES ({placeholders})", converted)


def copy_forecasts(connection: sqlite3.Connection, columns: list[str], timezone: ZoneInfo) -> None:
    source_columns = ["collected_at_utc", "forecast_time", *columns]
    placeholders = ", ".join("?" for _ in source_columns)
    target_names = ", ".join(f'"{column}"' for column in source_columns)
    rows = connection.execute(f"SELECT {target_names} FROM {LEGACY_FORECAST_TABLE}")
    converted = (
        (
            to_epoch(row["collected_at_utc"], None),
            to_epoch(row["forecast_time"], timezone),
            *(scale_value(row[column]) for column in columns),
        )
        for row in rows
    )
    connection.executemany(f"INSERT INTO {FORECAST_TABLE}_compact ({target_names}) VALUES ({placeholders})", converted)


def to_epoch(value: str, timezone: ZoneInfo | None) -> int:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        if timezone is None:
            raise ValueError(f"Timestamp has no timezone: {value!r}")
        timestamp = timestamp.replace(tzinfo=timezone)
    return round(timestamp.timestamp())


def scale_value(value: float | None) -> int | None:
    return None if value is None else round(value * SCALE)


def verify_snapshot(database_path: Path, snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    with sqlite3.connect(database_path) as connection:
        for table, key, columns in (
            (SAMPLE_TABLE, "samples", SAMPLE_SNAPSHOT_COLUMNS),
            (FORECAST_TABLE, "forecasts", FORECAST_SNAPSHOT_COLUMNS),
        ):
            for reference in snapshot[key]:
                epoch = to_epoch(reference["collected_at_utc"], None)
                if table == SAMPLE_TABLE:
                    where, params = "collected_at_utc = ?", (epoch,)
                else:
                    where = "collected_at_utc = ? AND forecast_time = ?"
                    params = (epoch, to_epoch(reference["forecast_time"], ZoneInfo("Europe/Dublin")))
                row = connection.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", params).fetchone()
                if row is None:
                    raise ValueError(f"Missing migrated {table} row for {reference['collected_at_utc']}")
                for column, value in zip(columns, row, strict=True):
                    if value != scale_value(reference[column]):
                        raise ValueError(f"{table}.{column} does not match snapshot at {reference['collected_at_utc']}")
    print("Snapshot verification passed")


def compact_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("VACUUM")


if __name__ == "__main__":
    main()
