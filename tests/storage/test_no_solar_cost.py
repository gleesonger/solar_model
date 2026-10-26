"""No Solar Cost uses house demand in collection, backfill and tariff updates."""

from dataclasses import replace
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from solar_model.config import load_config, TariffPeriod, TariffRule, TariffsConfig
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.migrations import open_database
from solar_model.storage.schema import SigenStorModbusSample
from tests.storage.test_grid_power_rollup import tariffs


def test_collection_uses_house_energy_and_never_falls_back_to_load_counter(tmp_path):
    database = open_database(tmp_path / "collection.db")
    try:
        previous = {}
        for minute, total in [(0, 100.), (1, 110.), (4, 120.)]:
            stamp = f"2026-10-26T00:{minute:02d}:00+00:00"
            database.save_modbus_sample({"plant_load_power_kw": (60., "kW", False),
                "plant_load_total_kwh": (total, "kWh", True)}, previous, (stamp, stamp), tariffs())
        with database.sessions() as session:
            rows = session.scalars(select(SigenStorModbusSample).order_by(SigenStorModbusSample.collected_at_utc)).all()
            assert rows[1].plant_load_total_kwh_period == 10.
            assert rows[1].house_load_kwh_period == 1.
            assert [row.no_solar_battery_import_cost_period for row in rows] == [None, .4, None]
    finally:
        database.close()


def old_database(path):
    database = open_database(path)
    with database.sessions.begin() as session:
        for minute, (house, rate) in enumerate([(1., .4), (.5, .2), (0., .3), (None, .5), (2., None), (3., .1234)]):
            session.add(SigenStorModbusSample(collected_at_utc=f"2026-10-26T00:{minute:02d}:00Z",
                import_tariff_band="fixed", house_load_kwh_period=house, import_rate=rate,
                plant_load_total_kwh_period=10., no_solar_battery_import_cost_period=9.,
                grid_import_cost_period=.75, grid_export_revenue_period=.25, net_cost_period=.5))
    database.rebuild_hourly_rollup(date(2026, 10, 26), date(2026, 10, 26), ZoneInfo("UTC"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE database_metadata SET value='0.3.0' WHERE key='database_version'")
    database.close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_migration_overwrites_raw_and_hourly_costs_atomically_and_runs_once(tmp_path, monkeypatch, interrupt):
    path = tmp_path / "old.db"
    old_database(path)
    if interrupt:
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise RuntimeError("interrupted rebuild")
            patch.setattr(SolarDatabase, "_insert_hourly_rollup", fail)
            with pytest.raises(RuntimeError, match="interrupted rebuild"):
                open_database(path)
        from solar_model.storage.connection import create_engine_for_database
        with create_engine_for_database(path).connect() as connection:
            assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar() == "0.3.0"
            assert connection.exec_driver_sql("SELECT SUM(no_solar_battery_import_cost_period) FROM sigenstor").scalar() == 540000
            assert connection.exec_driver_sql("SELECT SUM(no_solar_battery_import_cost_period) FROM sigenstor_hourly").scalar() == 540000
    database = open_database(path)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT no_solar_battery_import_cost_period FROM sigenstor ORDER BY collected_at_utc").scalars().all() == [4000, 1000, 0, None, None, 3702]
            assert connection.exec_driver_sql("SELECT SUM(no_solar_battery_import_cost_period) FROM sigenstor_hourly").scalar() == 8702
            assert connection.exec_driver_sql("SELECT value FROM database_metadata WHERE key='database_version'").scalar() == "0.3.1"
            assert connection.exec_driver_sql("SELECT SUM(net_cost_period), SUM(grid_import_cost_period), SUM(grid_export_revenue_period) FROM sigenstor").one() == (30000, 45000, 15000)
    finally:
        database.close()
    monkeypatch.setattr(SolarDatabase, "_insert_hourly_rollup", lambda *args: pytest.fail("repeated backfill"))
    open_database(path).close()


def test_tariff_recalculation_keeps_house_load_basis(tmp_path, monkeypatch):
    import solar_model.tools.recalculate_economics as recalc
    path = tmp_path / "tariffs.db"
    old_database(path)
    config = load_config()
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    rules = TariffsConfig((TariffPeriod("2000-01-01", (TariffRule(days, "00:00", "24:00", .6, "fixed"),),
                                       (TariffRule(days, "00:00", "24:00", .2),)),))
    monkeypatch.setattr(recalc, "load_config", lambda _: replace(config, timezone="UTC", tariffs=rules,
                       database=replace(config.database, path=str(path))))
    assert recalc.main(["--from", "2026-10-26", "--to", "2026-10-26"]) == 0
    database = open_database(path)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT no_solar_battery_import_cost_period FROM sigenstor ORDER BY collected_at_utc").scalars().all() == [6000, 3000, 0, None, 12000, 18000]
            assert connection.exec_driver_sql("SELECT SUM(no_solar_battery_import_cost_period) FROM sigenstor_hourly").scalar() == 39000
    finally:
        database.close()
