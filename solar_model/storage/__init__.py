"""Database API; open_database applies migrations and may backfill rollups."""
from solar_model.storage.schema import (
    column_name,
    ScaledInteger,
    EpochSeconds,
    Float,
    Base,
    SigenStorModbusSample,
    SigenStorDevice,
    DatabaseMetadata,
    ForecastLoadSample,
    ForecastSolarSample,
    CUMULATIVE_COLUMNS,
    PV_ARRAY_IDS,
    PV_ARRAY_POWER_COLUMNS,
    PV_ARRAY_ENERGY_COLUMNS,
    PV_ARRAY_COLUMNS,
    HOURLY_MEASUREMENT_COLUMNS,
    HOURLY_GRID_POWER_COLUMNS,
    HOURLY_VALID_COUNT_COLUMNS,
    HOURLY_ROLLUP_MARKER,
    SigenStorHourlySample,
)
from .connection import SolarDatabase, create_engine_for_database
from .migrations import open_database
