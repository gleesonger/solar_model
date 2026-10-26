from datetime import date, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from solar_model.config import ForecastModelConfig, ForecastModelsConfig
from solar_model.storage.schema import ForecastLoadSample, SigenStorHourlySample
from solar_model.storage.migrations import open_database
import solar_model.services.forecasting as model
import solar_model.dashboard.components.charts as charts
import solar_model.storage.queries.dashboard as data
from solar_model.storage.queries.recent import load_energy_summaries


def read_profile(db):
    with db.sessions() as session:
        return [(r.forecast_time, r.load_kwh, r.collected_at_utc) for r in session.scalars(
            select(ForecastLoadSample).order_by(ForecastLoadSample.forecast_time))]


@pytest.mark.parametrize("day,hours", [("2026-10-23", 24), ("2026-03-29", 23), ("2026-10-25", 25)])
def test_daily_profile_uses_prior_days_and_survives_restart(tmp_path, monkeypatch, day, hours):
    path = tmp_path / "load.db"
    db = open_database(path)
    zone = ZoneInfo("Europe/Dublin")
    target = date.fromisoformat(day)
    start, end = model._day_bounds(target, zone)
    with db.sessions.begin() as session:
        for stamp in pd.date_range(start - pd.Timedelta(days=2), start, freq="h", inclusive="left"):
            session.add(SigenStorHourlySample(hour_start_utc=stamp.isoformat(), import_tariff_band="day",
                sample_count=1, plant_load_total_kwh_period=1.))
        # These readings must never participate, even when starting late today.
        for stamp in pd.date_range(start, end, freq="h", inclusive="left"):
            session.add(SigenStorHourlySample(hour_start_utc=stamp.isoformat(), import_tariff_band="day",
                sample_count=1, plant_load_total_kwh_period=999.))
    as_of = start + pd.Timedelta(hours=12)
    assert model.refresh_daily_load_forecast(db, as_of, zone.key) == hours
    first = read_profile(db)
    assert len(first) == hours
    assert sum(row[1] for row in first) == hours
    assert all(row[1] == 1. for row in first)
    assert all(row[2] == as_of.isoformat() for row in first)
    # Raw storage remains scaled; timestamp identity preserves both autumn hours.
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT MIN(load_kwh) FROM forecast_load").scalar() == 10000
    db.close()
    restarted = open_database(path)
    monkeypatch.setattr(model, "load_hourly_load", Mock(side_effect=AssertionError("must not refit")))
    changed = ForecastModelsConfig(load=ForecastModelConfig(1., 1., 1.))
    assert model.refresh_daily_load_forecast(restarted, as_of + pd.Timedelta(hours=1), zone.key, models=changed) == 0
    assert read_profile(restarted) == first
    # A second writer also cannot overwrite or append to this frozen profile.
    assert restarted.save_daily_load_forecast(day, as_of.isoformat(), [{"forecast_time": start.isoformat(), "load_kwh": 500.}]) == 0
    assert read_profile(restarted) == first
    restarted.close()


def test_missing_history_is_frozen_as_missing_not_zero(tmp_path):
    db = open_database(tmp_path / "empty.db")
    try:
        assert model.refresh_daily_load_forecast(db, "2026-10-23T00:00Z", "UTC") == 24
        assert all(row[1] is None for row in read_profile(db))
        assert model.refresh_daily_load_forecast(db, "2026-10-24T00:00Z", "UTC") == 24
        assert len(read_profile(db)) == 48
    finally:
        db.close()


def test_saved_load_profile_flows_to_past_summaries_and_only_hourly_energy(tmp_path):
    path = str(tmp_path / "dashboard.db")
    db = open_database(path)
    try:
        day = date(2026, 10, 23)
        db.save_daily_load_forecast(day.isoformat(), "2026-10-23T00:15Z", [
            {"forecast_time": "2026-10-23T00:00Z", "load_kwh": 1.},
            {"forecast_time": "2026-10-23T01:00Z", "load_kwh": 2.},
        ])
        # SQL comparisons use elapsed energy, including half the current hour.
        frame = load_energy_summaries(path, "UTC", day, day, (), datetime(2026, 10, 23, 1, 30, tzinfo=ZoneInfo("UTC")),
            daily_start=day, monthly_start=day, lifetime_start=day)
        assert frame.load_forecast.tolist() == [2.] * 5
        bounds = data.analysis_date_bounds(day, day, ZoneInfo("UTC"))
        hourly = data.load_analysis_range(path, "UTC", bounds, "hour", 60, (), {}).energy
        assert hourly.load_forecast.iloc[:2].tolist() == [1., 2.]
        assert hourly.load_forecast.iloc[2:].isna().all()
        options = charts.energy_chart_options(hourly, "period")
        assert options["legend"]["selected"]["Load (Forecast)"] is False
        series = {item["name"]: item for item in options["series"]}
        assert series["Load (Forecast)"]["itemStyle"] == charts.forecast_item_style(charts.LOAD_COLOR)
        assert series["Load"]["itemStyle"] == {"color": "#6C757D"}
        assert series["House Load"]["itemStyle"] == {"color": charts.LOAD_COLOR}
        assert series["Load (Forecast)"]["itemStyle"]["decal"] == series["Solar (Forecast)"]["itemStyle"]["decal"]
        daily = data.load_analysis_range(path, "UTC", bounds, "day", 60, (), {}).energy
        assert "load_forecast" not in daily
        assert "Load (Forecast)" not in charts.energy_chart_options(daily, "period")["legend"]["data"]
    finally:
        db.close()


def test_failed_profile_insert_rolls_back_and_can_retry(tmp_path):
    db = open_database(tmp_path / "retry.db")
    try:
        rows = [
            {"forecast_time": "2026-10-23T00:00Z", "load_kwh": 1.},
            {"forecast_time": "2026-10-23T01:00Z", "load_kwh": object()},
        ]
        with pytest.raises(SQLAlchemyError):
            db.save_daily_load_forecast("2026-10-23", "2026-10-23T00:15Z", rows)
        assert not db.has_load_forecast("2026-10-23")
        rows[1]["load_kwh"] = 2.
        assert db.save_daily_load_forecast("2026-10-23", "2026-10-23T00:15Z", rows) == 2
        assert [row[1] for row in read_profile(db)] == [1., 2.]
    finally:
        db.close()
