"""R04: split grid signs before aggregation, including historical upgrades."""

from datetime import date
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config import TariffPeriod, TariffRule, TariffsConfig
from dashboard import data
from database import HOURLY_ROLLUP_MARKER, Base, DatabaseMetadata, SigenStorHourlySample, SigenStorModbusSample, SolarDatabase, open_database


def tariffs(split=False):
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    imports = (
        (TariffRule(days, "00:00", "10:01", .4, "a"), TariffRule(days, "10:01", "24:00", .4, "b"))
        if split else (TariffRule(days, "00:00", "24:00", .4, "fixed"),)
    )
    return TariffsConfig((TariffPeriod("2020-01-01", imports, (TariffRule(days, "00:00", "24:00", .2),)),))


def record(database, previous, stamp, power, rules):
    values = {} if power is None else {"plant_grid_power": (power, "kW", False)}
    database.save_modbus_sample(values, previous, (stamp, stamp), rules)


@pytest.mark.parametrize("powers,expected", [
    ([2., -2.], (1., 1.)), ([4., -2.], (2., 1.)),
    ([2., 4.], (3., 0.)), ([-2., -4.], (0., 3.)),
    ([0., 0.], (0., 0.)), ([2., None, -2.], (1., 1.)),
    ([None, None], (None, None)), ([2., None], (2., 0.)),
])
@pytest.mark.parametrize("split_tariff", [False, True])
def test_grid_means_match_independent_expectations_for_raw_and_hourly(tmp_path, powers, expected, split_tariff):
    path = tmp_path / "grid.db"
    database = open_database(path)
    try:
        previous = {}
        for day in [14, 15]:
            for minute, power in enumerate(powers):
                record(database, previous, f"2026-10-{day}T10:{minute:02d}:00+00:00", power, tariffs(split_tariff))
        bounds = data.analysis_date_bounds(date(2026, 10, 14), date(2026, 10, 15), ZoneInfo("UTC"))
        selections = (("power_grid_import", None), ("power_grid_export", None))
        for source in ["minute", "hour"]:
            result = data.load_chart_data(str(path), "UTC", bounds, "hour", "average", (), {}, selections, sample_resolution=source)
            position = result["periods"].index("10:00")
            observed = [series["data"][position] for series in result["series"]]
            for actual, wanted in zip(observed, expected):
                assert actual is None if wanted is None else actual == pytest.approx(wanted)
        with database.sessions() as session:
            rows = session.scalars(select(SigenStorHourlySample)).all()
            assert sum(row.grid_power_sample_count for row in rows) == 2 * sum(power is not None for power in powers)
            legacy_power, _ = data.average_hourly_rollup_telemetry(rows, ZoneInfo("UTC"), {})
            row = legacy_power.loc[legacy_power.time == "10:00"].iloc[0]
            for column, wanted in zip(["grid_import", "grid_export"], expected):
                assert pd.isna(row[column]) if wanted is None else row[column] == pytest.approx(wanted)
        # Range rebuilding uses the same statistics as incremental collection.
        database.rebuild_hourly_rollup(date(2026, 10, 14), date(2026, 10, 15), ZoneInfo("UTC"))
        rebuilt = data.load_chart_data(str(path), "UTC", bounds, "hour", "average", (), {}, selections)
        assert rebuilt == result
    finally:
        database.close()


def old_database(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine, tables=[SigenStorModbusSample.__table__, DatabaseMetadata.__table__])
    with Session(engine) as session:
        for minute, power in [(0, 2.), (1, -2.)]:
            session.add(SigenStorModbusSample(collected_at_utc=f"2026-10-14T10:0{minute}:00+00:00", plant_grid_power_kw=power, import_tariff_band="fixed"))
        session.add(DatabaseMetadata(key="hourly_rollup_initialized_v1", value="1"))
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sigenstor_hourly (hour_start_utc INTEGER NOT NULL, import_tariff_band TEXT NOT NULL, sample_count INTEGER NOT NULL, plant_grid_power_kw INTEGER, PRIMARY KEY(hour_start_utc, import_tariff_band))")
        connection.exec_driver_sql("INSERT INTO sigenstor_hourly SELECT (collected_at_utc / 3600) * 3600, 'fixed', 2, 0 FROM sigenstor LIMIT 1")
    engine.dispose()


@pytest.mark.parametrize("interrupt", [False, True])
def test_existing_v1_database_gets_atomic_v2_rebuild_once(tmp_path, monkeypatch, interrupt):
    path = tmp_path / "old.db"
    old_database(path)
    if interrupt:
        original = SolarDatabase._insert_hourly_rollup

        def fail_after_insert(connection, *args, **kwargs):
            original(connection, *args, **kwargs)
            raise RuntimeError("interrupted stats upgrade")

        with monkeypatch.context() as patch:
            patch.setattr(SolarDatabase, "_insert_hourly_rollup", staticmethod(fail_after_insert))
            with pytest.raises(RuntimeError):
                open_database(path)
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key=?", (HOURLY_ROLLUP_MARKER,)).scalar_one_or_none() is None
            assert connection.exec_driver_sql("SELECT sample_count, plant_grid_import_power_kw FROM sigenstor_hourly").one() == (2, None)
        engine.dispose()
    database = open_database(path)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT sample_count, grid_power_sample_count, plant_grid_power_kw, plant_grid_import_power_kw, plant_grid_export_power_kw FROM sigenstor_hourly").one() == (2, 2, 0, 20000, 20000)
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM sigenstor").scalar_one() == 2
    record(database, {}, "2026-10-14T10:02:00+00:00", 4., tariffs())
    database.close()
    with monkeypatch.context() as patch:
        patch.setattr(SolarDatabase, "_insert_hourly_rollup", Mock(side_effect=AssertionError("must not rebuild marked history")))
        database = open_database(path)
        with database.sessions() as session:
            row = session.scalars(select(SigenStorHourlySample)).one()
            assert (row.sample_count, row.grid_power_sample_count) == (3, 3)
            assert (row.plant_grid_import_power_kw, row.plant_grid_export_power_kw) == (6., 2.)
        database.close()
