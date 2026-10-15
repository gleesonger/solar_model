"""Regressions for review R01-R03 using isolated databases and fake readings."""

from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from config import ModbusConfig
from dashboard.live import (
    LIVE_BATTERY_METRICS, LIVE_POWER_METRICS, LIVE_TODAY_METRICS, LivePowerCollector,
)
from database import (
    Base, DatabaseMetadata, ForecastSolarSample, SigenStorHourlySample,
    SigenStorModbusSample, SolarDatabase, open_database,
)
from forecast.operational_model import _today_residual, refresh_adjusted_forecast


@pytest.mark.parametrize("field,bad", [
    ("plant_pv_power_kw", np.nan), ("plant_pv_power_kw", np.inf),
    ("plant_pv_power_kw", -np.inf), ("plant_pv_power_kw", None),
    ("baseline_kw", np.nan), ("baseline_kw", np.inf),
    ("raw_total_kw", np.nan), ("raw_total_kw", 0),
])
def test_residual_excludes_invalid_observations(field, bad):
    valid = dict(target_hour=pd.Timestamp("2026-01-04T09:00Z"), raw_total_kw=1.,
                 baseline_kw=1., plant_pv_power_kw=2.)
    issued = pd.DataFrame([valid, {**valid, field: bad}])
    assert _today_residual(issued, {9: (1., 1.)}, pd.Timestamp("2026-01-04T10:00Z")) == pytest.approx((1., 1 / 3, 1))


@pytest.mark.parametrize("sigma", [0., -1., np.nan, np.inf])
def test_residual_without_usable_distribution_is_neutral(sigma):
    issued = pd.DataFrame([dict(target_hour=pd.Timestamp("2026-01-04T09:00Z"),
                                raw_total_kw=1., baseline_kw=1., plant_pv_power_kw=2.)])
    assert _today_residual(issued, {9: (1., sigma)}, pd.Timestamp("2026-01-04T10:00Z")) == (0., 0., 0)


def test_missing_actual_hour_still_writes_finite_adjusted_forecast(tmp_path):
    database = open_database(tmp_path / "forecast.db")
    try:
        with database.sessions.begin() as session:
            for day, power in [(1, .5), (2, 1.), (3, 1.5)]:
                for hour in [9, 10]:
                    session.add(SigenStorModbusSample(
                        collected_at_utc=f"2026-01-{day:02d}T{hour:02d}:00:00+00:00",
                        plant_pv_power_kw=power,
                    ))
            for hour in [9, 10]:
                session.add(ForecastSolarSample(
                    collected_at_utc="2026-01-03T23:00:00+00:00",
                    forecast_time=f"2026-01-04T{hour:02d}:00:00+00:00", panel_1_raw_watts=1000.,
                ))
        refresh_adjusted_forecast(database, "2026-01-04T10:15Z", "UTC")
        with database.sessions() as session:
            rows = session.scalars(select(ForecastSolarSample).order_by(ForecastSolarSample.forecast_time)).all()
            assert [row.panel_1_raw_watts for row in rows] == [1000., 1000.]
            assert rows[1].panel_1_adj_watts == 1000.
            assert rows[1].panel_2_adj_watts == 0.
            assert rows[1].panel_1_adj_watt_hours == 1000.
    finally:
        database.close()


def live_readings():
    readings = {key: (1., "", False) for key in LIVE_POWER_METRICS | LIVE_BATTERY_METRICS | LIVE_TODAY_METRICS}
    readings.update({
        "plant_grid_import_total_kwh": (110., "kWh", True),
        "plant_grid_export_total_kwh": (55., "kWh", True),
        "inverter_pv1_voltage": (1000., "V", False), "inverter_pv1_current": (1., "A", False),
        "inverter_pv2_voltage": (1000., "V", False), "inverter_pv2_current": (1., "A", False),
    })
    return readings


@pytest.mark.parametrize("midnight", [False, True])
@pytest.mark.parametrize("failed_loader", ["grid", "pv"])
def test_live_day_baseline_failure_retries_without_publishing_partial_state(tmp_path, monkeypatch, midnight, failed_loader):
    from dashboard import live
    collector = LivePowerCollector(ModbusConfig("127.0.0.1", 502, 1., 1), "Europe/Dublin", str(tmp_path / "unused.db"), 1., ("pv1", "pv2"))
    grid = Mock(return_value={"today_grid_import": 100., "today_grid_export": 50.})
    # The loader returns independent baseline dictionaries on each call.
    grid.side_effect = lambda *args: {"today_grid_import": 100., "today_grid_export": 50.}
    pv = Mock(return_value={1: 2., 2: 1.})
    monkeypatch.setattr(live.data, "load_grid_energy_day_start", grid)
    monkeypatch.setattr(live.data, "load_pv_array_energy_day_totals", pv)
    monkeypatch.setattr(live, "read_registers", lambda *args: live_readings())
    clock = Mock(return_value=("2026-10-14T22:59:00+00:00", "2026-10-14T23:59:00+01:00"))
    monkeypatch.setattr(live, "timestamps", clock)
    if midnight:
        assert collector._collect_once(object())
    prior_snapshot = collector.snapshot()
    prior_day = collector._pv_energy_day
    clock.return_value = ("2026-10-14T23:01:00+00:00", "2026-10-15T00:01:00+01:00")
    loader = grid if failed_loader == "grid" else pv
    healthy = {"today_grid_import": 100., "today_grid_export": 50.} if failed_loader == "grid" else {1: 2., 2: 1.}
    loader.side_effect = [RuntimeError("temporary baseline failure"), healthy]
    assert not collector._collect_once(object())
    assert collector.snapshot() == prior_snapshot
    assert collector._pv_energy_day == prior_day
    assert collector._grid_energy_day == prior_day
    assert collector._collect_once(object())
    assert collector.snapshot().values["today_grid_import"] == 10.
    assert collector.snapshot().values["today_grid_export"] == 5.
    assert collector._pv_energy_day == collector._grid_energy_day == "2026-10-15"
    assert collector._pv_energy_live == {1: 0., 2: 0.}


def test_failed_second_pv_read_does_not_double_count_retry(tmp_path, monkeypatch):
    from dashboard import live
    collector = LivePowerCollector(ModbusConfig("127.0.0.1", 502, 1., 1), "UTC", str(tmp_path / "unused.db"), 1., ("pv1", "pv2"))
    monkeypatch.setattr(live.data, "load_grid_energy_day_start", lambda *args: {})
    monkeypatch.setattr(live.data, "load_pv_array_energy_day_totals", lambda *args: {1: 0., 2: 0.})
    collector._power_values(live_readings(), "2026-10-14T12:00:00+00:00")
    invalid = {**live_readings(), "inverter_pv2_current": ("invalid", "A", False)}
    with pytest.raises(ValueError):
        collector._power_values(invalid, "2026-10-14T12:01:00+00:00")
    assert collector._pv_energy_live == {1: 0., 2: 0.}
    collector._power_values(live_readings(), "2026-10-14T12:02:00+00:00")
    assert collector._pv_energy_live == pytest.approx({1: 2 / 60, 2: 2 / 60})


def legacy_database(path, *, hourly_exists):
    engine = create_engine(f"sqlite:///{path}")
    tables = [SigenStorModbusSample.__table__]
    if hourly_exists:
        tables.append(SigenStorHourlySample.__table__)
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        for minute, power in [(0, 1.), (20, 2.)]:
            session.add(SigenStorModbusSample(collected_at_utc=f"2026-10-14T10:{minute:02d}:00+00:00", import_tariff_band="day", plant_pv_power_kw=power))
        if hourly_exists:
            session.add(SigenStorHourlySample(hour_start_utc="2026-10-14T10:00:00+00:00", import_tariff_band="day", sample_count=99, plant_pv_power_kw=99.))
        session.commit()
    engine.dispose()


@pytest.mark.parametrize("hourly_exists", [False, True])
@pytest.mark.parametrize("after_insert", [False, True])
def test_interrupted_hourly_initialization_recovers_exactly_once(tmp_path, monkeypatch, hourly_exists, after_insert):
    path = tmp_path / "legacy.db"
    legacy_database(path, hourly_exists=hourly_exists)
    original = SolarDatabase._insert_hourly_rollup

    def interrupted(connection, *args, **kwargs):
        if after_insert:
            original(connection, *args, **kwargs)
        raise RuntimeError("injected initialization interruption")

    with monkeypatch.context() as patch:
        patch.setattr(SolarDatabase, "_insert_hourly_rollup", staticmethod(interrupted))
        with pytest.raises(RuntimeError):
            open_database(path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert connection.execute(select(DatabaseMetadata.value)).scalar_one_or_none() is None
        assert inspect(connection).has_table("sigenstor_hourly") == hourly_exists
        if hourly_exists:
            assert connection.execute(select(SigenStorHourlySample.sample_count)).scalar_one() == 99
    engine.dispose()
    database = open_database(path)
    with database.sessions() as session:
        row = session.scalars(select(SigenStorHourlySample)).one()
        assert (row.sample_count, row.plant_pv_power_kw) == (2, 3.)
        assert len(session.scalars(select(SigenStorModbusSample)).all()) == 2
    database.close()
    with monkeypatch.context() as patch:
        patch.setattr(SolarDatabase, "_insert_hourly_rollup", Mock(side_effect=AssertionError("must not rescan initialized history")))
        for _ in range(2):
            open_database(path).close()


def test_missing_hourly_table_is_rebuilt_even_with_old_completion_marker(tmp_path, monkeypatch):
    path = tmp_path / "marked.db"
    legacy_database(path, hourly_exists=False)
    database = open_database(path)
    with database.engine.begin() as connection:
        SigenStorHourlySample.__table__.drop(connection)
    database.close()
    with monkeypatch.context() as patch:
        patch.setattr(SolarDatabase, "_insert_hourly_rollup", Mock(side_effect=RuntimeError("interrupted recreation")))
        with pytest.raises(RuntimeError):
            open_database(path)
    database = open_database(path)
    with database.sessions() as session:
        row = session.scalars(select(SigenStorHourlySample)).one()
        assert (row.sample_count, row.plant_pv_power_kw) == (2, 3.)
    database.close()
