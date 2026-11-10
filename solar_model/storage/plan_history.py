"""Atomic plan publication and submission-time retention in the telemetry DB."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4
import zlib

from sqlalchemy import inspect, select

from solar_model.config import PlanHistoryConfig
from solar_model.storage.connection import create_engine_for_database
from solar_model.storage.schema import DatabaseMetadata, PlanHistory


@dataclass(frozen=True)
class StoredPlan:
    snapshot_id: str
    saved_at: datetime
    metadata: dict[str, Any]
    sections: dict[tuple[str, str], dict[str, Any]]


def latest_plan_id(database_path: str) -> str | None:
    table = PlanHistory.__table__
    with create_engine_for_database(database_path).connect() as connection:
        return connection.scalar(select(table.c.snapshot_id).where(table.c.view == "metadata").order_by(table.c.saved_at.desc(), table.c.snapshot_id.desc()).limit(1))


def write_planner_status(database_path: str, status: dict[str, Any]) -> None:
    from sqlalchemy.dialects.sqlite import insert
    statement = insert(DatabaseMetadata).values(key="planning_service_status", value=json.dumps(status))
    with create_engine_for_database(database_path).begin() as connection:
        connection.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"value": statement.excluded.value}))


def read_planner_status(database_path: str) -> dict[str, Any] | None:
    with create_engine_for_database(database_path).connect() as connection:
        value = connection.scalar(select(DatabaseMetadata.value).where(DatabaseMetadata.key == "planning_service_status"))
    return json.loads(value) if value else None


def encode(value: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def decode(value: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(value))


def save_plan(database_path: str, saved_at: datetime, metadata: dict[str, Any], sections: dict[tuple[str, str], dict[str, Any]], retention: PlanHistoryConfig) -> str:
    """Commit all sections and retention together; callers publish only this ID."""
    stamp = int(saved_at.timestamp() * 1_000_000)
    frequency = retention.retain_snapshots_frequency_mins * 60_000_000
    slot = stamp // frequency
    snapshot_id = uuid4().hex
    table = PlanHistory.__table__
    records = {("metadata", ""): metadata, **sections}
    rows = [{"snapshot_id": snapshot_id, "view": view, "scenario": scenario, "saved_at": stamp, "slot": slot, "payload": encode(value)} for (view, scenario), value in records.items()]
    engine = create_engine_for_database(database_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(table.insert(), rows)
        # Re-bucket old rows when settings change, including pre-config snapshots.
        key = "plan_history_frequency_mins"
        previous_frequency = connection.scalar(select(DatabaseMetadata.value).where(DatabaseMetadata.key == key))
        if previous_frequency != str(retention.retain_snapshots_frequency_mins):
            connection.exec_driver_sql("UPDATE plan_history SET slot = CAST(saved_at / ? AS INTEGER)", (frequency,))
            connection.exec_driver_sql("INSERT INTO database_metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(retention.retain_snapshots_frequency_mins)))
        # Pick the first successful run in each configured UTC slot before deleting.
        # Rank in SQL so cleanup does not fetch the full retained history.
        connection.exec_driver_sql(
            "DELETE FROM plan_history WHERE snapshot_id <> ? AND ("
            "saved_at < ? OR snapshot_id NOT IN ("
            "SELECT snapshot_id FROM (SELECT snapshot_id, "
            "ROW_NUMBER() OVER (PARTITION BY slot ORDER BY saved_at, snapshot_id) AS position "
            "FROM plan_history WHERE view = 'metadata') WHERE position = 1))",
            (snapshot_id, stamp - retention.retain_snapshots_period_days * 86_400_000_000),
        )
    return snapshot_id


def list_plans(database_path: str, start: datetime | None = None, end: datetime | None = None) -> list[StoredPlan]:
    engine = create_engine_for_database(database_path)
    if not inspect(engine).has_table("plan_history"):
        return []
    table = PlanHistory.__table__
    query = select(table.c.snapshot_id, table.c.saved_at, table.c.payload).where(table.c.view == "metadata")
    if start is not None:
        query = query.where(table.c.saved_at >= int(start.timestamp() * 1_000_000))
    if end is not None:
        query = query.where(table.c.saved_at < int(end.timestamp() * 1_000_000))
    query = query.order_by(table.c.saved_at.desc(), table.c.snapshot_id.desc())
    with engine.connect() as connection:
        return [StoredPlan(row.snapshot_id, datetime.fromtimestamp(row.saved_at / 1_000_000, timezone.utc), decode(row.payload), {}) for row in connection.execute(query)]


def load_plan(database_path: str, snapshot_id: str | None = None) -> StoredPlan | None:
    engine = create_engine_for_database(database_path)
    if not inspect(engine).has_table("plan_history"):
        return None
    table = PlanHistory.__table__
    with engine.connect() as connection:
        # A single SELECT keeps metadata and sections on one SQLite read snapshot.
        if snapshot_id is None:
            latest = select(table.c.snapshot_id).where(table.c.view == "metadata").order_by(table.c.saved_at.desc(), table.c.snapshot_id.desc()).limit(1).scalar_subquery()
            query = select(table).where(table.c.snapshot_id == latest)
        else:
            query = select(table).where(table.c.snapshot_id == snapshot_id)
        rows = connection.execute(query).mappings().all()
    if not rows:
        return None
    metadata = next(row for row in rows if row["view"] == "metadata")
    value = decode(metadata["payload"])
    if value.get("version") != 1:
        raise ValueError("Unsupported saved plan format")
    sections = {(row["view"], row["scenario"]): decode(row["payload"]) for row in rows if row["view"] != "metadata"}
    return StoredPlan(metadata["snapshot_id"], datetime.fromtimestamp(metadata["saved_at"] / 1_000_000, timezone.utc), value, sections)
