from __future__ import annotations
from pathlib import Path
from typing import cast
from sqlalchemy import Table, inspect
from sqlalchemy.engine import Engine

from solar_model.storage.schema import (
    column_name,
    Base,
    SigenStorModbusSample,
    SigenStorDevice,
    DatabaseMetadata,
    ForecastHouseLoadSample,
    ForecastSolarSample,
    PV_ARRAY_IDS,
    SigenStorHourlySample,
)
from solar_model.storage.connection import SolarDatabase, create_engine_for_database
from solar_model.storage.measurements import house_load_energy

DATABASE_VERSION = "0.3.0"

def open_database(path: str | Path) -> SolarDatabase:
    engine = create_engine_for_database(path)
    if inspect(engine).has_table(DatabaseMetadata.__tablename__):
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT value FROM database_metadata WHERE key = 'database_version'"
            ).scalar_one_or_none()
        if version not in (None, "0.1.0", "0.2.0", DATABASE_VERSION):
            raise RuntimeError(f"Unsupported database version: {version!r}")
    _rename_forecast_solar_raw_columns(engine)
    _rename_sigenstor_battery_available_discharge_column(engine)
    inspector = inspect(engine)
    hourly_table_missing = not inspector.has_table(SigenStorHourlySample.__table__.name)
    # Create/migrate the raw source first. The hourly table mirrors its
    # measurement columns, so it must only be created after this step.
    Base.metadata.create_all(
        engine,
        tables=(
            SigenStorModbusSample.__table__,
            SigenStorDevice.__table__,
            DatabaseMetadata.__table__,
            ForecastSolarSample.__table__,
        ),
    )
    _add_missing_columns(engine, SigenStorModbusSample)
    _add_missing_columns(engine, SigenStorDevice)
    _add_missing_columns(engine, ForecastSolarSample)
    _drop_columns(engine, SigenStorModbusSample, ("raw_registers_json",))
    _drop_columns(engine, ForecastSolarSample, ("raw_json",))
    database = SolarDatabase(engine)
    if not hourly_table_missing:
        _add_missing_columns(engine, SigenStorHourlySample)
    database.initialize_hourly_rollup()
    _migrate_house_load(database)
    _migrate_house_load_forecast(database)
    return database


def _migrate_house_load(database: SolarDatabase) -> None:
    """Backfill house energy and publish database version in one transaction."""
    with database.engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        version = connection.exec_driver_sql(
            "SELECT value FROM database_metadata WHERE key = 'database_version'"
        ).scalar_one_or_none()
        if version in ("0.2.0", DATABASE_VERSION):
            return
        if version not in (None, "0.1.0"):
            raise RuntimeError(f"Unsupported database version: {version!r}")
        rows = connection.exec_driver_sql(
            "SELECT collected_at_utc, plant_load_power_kw FROM sigenstor ORDER BY collected_at_utc"
        ).fetchall()
        previous_stamp, previous_power = None, None
        updates = []
        for stamp, raw_power in rows:
            power = raw_power / 10000 if raw_power is not None else None
            energy = house_load_energy(
                previous_power, power, stamp - previous_stamp if previous_stamp is not None else 0,
            )
            updates.append((round(energy * 10000) if energy is not None else None, stamp))
            previous_stamp, previous_power = stamp, power
        if updates:
            connection.exec_driver_sql(
                "UPDATE sigenstor SET house_load_kwh_period = ? WHERE collected_at_utc = ?", updates,
            )
        connection.execute(SigenStorHourlySample.__table__.delete())
        database._insert_hourly_rollup(connection)
        connection.exec_driver_sql(
            "INSERT INTO database_metadata (key, value) VALUES ('database_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("0.2.0",),
        )


def _migrate_house_load_forecast(database: SolarDatabase) -> None:
    """Replace the undeployed counter-based forecast table with house forecasts."""
    with database.engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        ForecastHouseLoadSample.__table__.create(connection, checkfirst=True)
        if connection.exec_driver_sql(
            "SELECT value FROM database_metadata WHERE key = 'database_version'"
        ).scalar_one_or_none() == DATABASE_VERSION:
            return
        connection.exec_driver_sql("DROP TABLE IF EXISTS forecast_load")
        connection.exec_driver_sql(
            "INSERT INTO database_metadata (key, value) VALUES ('database_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (DATABASE_VERSION,),
        )

def _add_missing_columns(engine: Engine, model: type[Base]) -> None:
    table = cast(Table, model.__table__)
    existing_columns = {column["name"] for column in inspect(engine).get_columns(table.name)}
    missing_columns = [column for column in table.columns if column.name not in existing_columns]
    if not missing_columns:
        return

    preparer = engine.dialect.identifier_preparer
    table_name = preparer.quote(table.name)
    with engine.begin() as connection:
        for column in missing_columns:
            column_name = preparer.quote(column.name)
            column_type = column.type.compile(dialect=engine.dialect)
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

def _rename_sigenstor_battery_available_discharge_column(engine: Engine) -> None:
    """Preserve inverter availability history under the plant-level name."""
    old_name = "inverter_battery_available_discharge_kwh"
    new_name = "plant_battery_available_discharge_kwh"
    preparer = engine.dialect.identifier_preparer
    for table_name in (SigenStorModbusSample.__table__.name, SigenStorHourlySample.__table__.name):
        if not inspect(engine).has_table(table_name):
            continue
        existing_columns = {
            column["name"] for column in inspect(engine).get_columns(table_name)
        }
        if old_name not in existing_columns or new_name in existing_columns:
            continue
        quoted_table = preparer.quote(table_name)
        quoted_old = preparer.quote(old_name)
        quoted_new = preparer.quote(new_name)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_old} TO {quoted_new}"
            )

def _rename_forecast_solar_raw_columns(engine: Engine) -> None:
    """Migrate the pre-adjustment forecast names without losing API values.

    SQLite's native rename retains both the values and the scaled-integer
    storage type.  This must run before ``create_all``/``_add_missing_columns``
    add the new adjusted columns.  A partially completed old deployment that
    already has both names is repaired by filling only absent raw values.
    """
    table = ForecastSolarSample.__table__
    if not inspect(engine).has_table(table.name):
        return
    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table.name)
    legacy_to_raw = {
        f"panel_{panel_id}_{measure}": f"panel_{panel_id}_raw_{measure}"
        for panel_id in PV_ARRAY_IDS
        for measure in ("watts", "watt_hours", "watt_hours_day")
    }
    with engine.begin() as connection:
        existing = {
            column["name"] for column in inspect(connection).get_columns(table.name)
        }
        for legacy, raw in legacy_to_raw.items():
            if legacy not in existing:
                continue
            quoted_legacy = preparer.quote(legacy)
            quoted_raw = preparer.quote(raw)
            if raw not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_legacy} TO {quoted_raw}"
                )
                existing.remove(legacy)
                existing.add(raw)
                continue

            # This state can only result from a previous non-atomic/manual
            # migration.  Preserve the raw column's explicit values and fill
            # only gaps from the legacy column before removing the duplicate.
            connection.exec_driver_sql(
                f"UPDATE {quoted_table} SET {quoted_raw} = {quoted_legacy} "
                f"WHERE {quoted_raw} IS NULL"
            )
            conflict = connection.exec_driver_sql(
                f"SELECT 1 FROM {quoted_table} WHERE {quoted_raw} IS NOT NULL "
                f"AND {quoted_legacy} IS NOT NULL AND {quoted_raw} != {quoted_legacy} LIMIT 1"
            ).first()
            if conflict is not None:
                raise RuntimeError(
                    f"refusing to drop conflicting forecast columns {legacy!r} and {raw!r}"
                )
            connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_legacy}")
            existing.remove(legacy)

def _drop_columns(engine: Engine, model: type[Base], column_names: tuple[str, ...]) -> None:
    table = cast(Table, model.__table__)
    table_name = table.name
    existing_columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
    columns_to_drop = [name for name in column_names if name in existing_columns]
    if not columns_to_drop:
        return

    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    with engine.begin() as connection:
        for name in columns_to_drop:
            connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {preparer.quote(name)}")
