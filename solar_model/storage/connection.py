from __future__ import annotations
from functools import cache
import logging
from pathlib import Path
from time import perf_counter
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from solar_model.logging_setup import LOGGER

from solar_model.storage.forecasts import ForecastStorage
from solar_model.storage.measurements import MeasurementStorage

class SolarDatabase(ForecastStorage, MeasurementStorage):

    def __init__(self, engine: Engine):
        self.engine = engine
        self.sessions = sessionmaker(engine)

    def close(self) -> None:
        self.engine.dispose()

@cache
def create_engine_for_database(path: str | Path) -> Engine:
    engine = create_engine(f"sqlite:///{Path(path)}", future=True)

    def is_read_query(statement: str) -> bool:
        return statement.lstrip().upper().startswith(("SELECT", "WITH", "EXPLAIN"))

    @event.listens_for(engine, "before_cursor_execute")

    def log_sql_start(connection, cursor, statement, parameters, context, executemany) -> None:
        if LOGGER.isEnabledFor(logging.DEBUG) and is_read_query(statement):
            context._solar_sql_started_at = perf_counter()

    @event.listens_for(engine, "after_cursor_execute")

    def log_sql_end(connection, cursor, statement, parameters, context, executemany) -> None:
        if hasattr(context, "_solar_sql_started_at"):
            elapsed = perf_counter() - context._solar_sql_started_at
            LOGGER.debug("SQL query (%.3fs): %s", elapsed, statement)
    return engine
