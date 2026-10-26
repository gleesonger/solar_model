from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from solar_model.config import SolarArrayConfig
import solar_model.storage.queries.dashboard as data
import solar_model.dashboard.snapshot as snapshot
from solar_model.storage.schema import Base, ForecastSolarSample, SigenStorHourlySample, SigenStorModbusSample
from solar_model.storage.connection import create_engine_for_database


ARRAYS = tuple(SolarArrayConfig(panel, str(panel), 0, 0, 0, 0, 1) for panel in (1, 2))


@pytest.fixture
def summary_db(tmp_path):
    path = str(tmp_path / "summaries.db")
    engine = create_engine_for_database(path)
    Base.metadata.create_all(engine)
    yield path, engine
    engine.dispose()


def freeze(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(data, "datetime", Clock)


def reference_daily(engine, zone, first, last, arrays, now):
    """Independent ORM/Python pipeline using the retained forecast aggregator."""
    with Session(engine) as session:
        actuals = list(session.scalars(select(SigenStorHourlySample)))
        forecasts = list(session.scalars(select(ForecastSolarSample).order_by(ForecastSolarSample.collected_at_utc)))
    fields = {
        "solar_actual": "plant_pv_total_kwh_period", "load": "plant_load_total_kwh_period",
        "house_load": "house_load_kwh_period",
        "grid_import": "plant_grid_import_total_kwh_period", "grid_export": "plant_grid_export_total_kwh_period",
        "grid_import_cost": "grid_import_cost_period", "grid_export_revenue": "grid_export_revenue_period",
        "net_cost": "net_cost_period", "no_solar_battery_import_cost": "no_solar_battery_import_cost_period",
    }
    rows = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        actual = [row for row in actuals if data.parse_time(row.hour_start_utc, zone).date() == day]
        vintage = next((row.collected_at_utc for row in forecasts if data.parse_time(row.collected_at_utc, zone).date() == day), None)
        selected = [row for row in forecasts if row.collected_at_utc == vintage]
        forecast = data.aggregate_forecast(selected, datetime.combine(day, time.min, zone), arrays, fill_missing=False)
        if day == now.date():
            forecast = data.scale_forecast_to_elapsed_time(forecast, now)
        values = {}
        for name, field in fields.items():
            present = [getattr(row, field) for row in actual if getattr(row, field) is not None]
            values[name] = sum(present) if present else np.nan
        rows.append({"date": day.isoformat(), **values, "solar_forecast": forecast["forecast_total"].sum(min_count=1)})
    return pd.DataFrame(rows)[["date", *data.ENERGY_SUMMARY_COLUMNS]]


@pytest.mark.parametrize("zone_name,day", [("Europe/Dublin", "2026-03-29"), ("Europe/Dublin", "2026-10-25"), ("Asia/Kolkata", "2026-09-10")])
@pytest.mark.parametrize("arrays", [ARRAYS[:1], ARRAYS, ()])
def test_daily_matches_python_across_dst_nulls_resets_and_vintages(summary_db, monkeypatch, zone_name, day, arrays):
    path, engine = summary_db
    zone = ZoneInfo(zone_name)
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    now = start.replace(hour=12, minute=23, second=45)
    freeze(monkeypatch, now)
    epoch = int(start.timestamp())
    with Session(engine) as session, session.begin():
        for hour in range(-3, 53):
            for band, value in [("a", 1.25), ("b", None if hour % 3 == 0 else 2.5)]:
                session.add(SigenStorHourlySample(hour_start_utc=epoch + hour * 3600, import_tariff_band=band,
                    sample_count=1, plant_pv_total_kwh_period=value, plant_load_total_kwh_period=value,
                    plant_grid_import_total_kwh_period=value, plant_grid_export_total_kwh_period=value,
                    grid_import_cost_period=value, grid_export_revenue_period=value,
                    net_cost_period=-value if value else None, no_solar_battery_import_cost_period=value))
        for collected in [epoch - 1, epoch + 900, epoch + 7200]:
            for hour, value in [(-1, 10), (0, 100), (1, None), (2, 300), (3, 200), (12, 500), (23, 1000), (24, 1100), (25, 1200), (36, -10), (37, 50)]:
                session.add(ForecastSolarSample(collected_at_utc=collected, forecast_time=epoch + hour * 3600,
                    panel_1_adj_watt_hours=value, panel_2_adj_watt_hours=None if hour % 2 else (value or 0) * 2,
                    panel_3_adj_watt_hours=9999))
        # A selected vintage with configured values solely outside the date
        # window means zero, whereas no configured values means missing.
        session.add(ForecastSolarSample(collected_at_utc=epoch + 3 * 86400 + 7200,
            forecast_time=epoch + 50 * 86400, panel_1_adj_watt_hours=1000))
    end = start.date() + timedelta(days=5)
    expected = reference_daily(engine, zone, start.date(), end, arrays, now)
    actual = data.load_daily_energy_totals(path, zone_name, start.date(), end, arrays)
    pd.testing.assert_frame_equal(actual.fillna(np.nan), expected.fillna(np.nan), check_dtype=False, rtol=1e-9, atol=1e-9)
    if arrays:
        assert actual.loc[3, "solar_forecast"] == 0
        assert pd.isna(actual.loc[4, "solar_forecast"])


def test_first_vintage_is_not_replaced_when_configured_panels_are_missing(summary_db, monkeypatch):
    path, engine = summary_db
    now = datetime(2026, 9, 10, 14, tzinfo=ZoneInfo("UTC"))
    freeze(monkeypatch, now)
    with Session(engine) as session, session.begin():
        session.add(ForecastSolarSample(collected_at_utc=now.replace(hour=1).isoformat(), forecast_time=now.isoformat(), panel_3_adj_watt_hours=1000))
        session.add(ForecastSolarSample(collected_at_utc=now.replace(hour=2).isoformat(), forecast_time=now.isoformat(), panel_1_adj_watt_hours=2000))
    result = data.load_daily_energy_totals(path, "UTC", now.date(), now.date(), ARRAYS)
    assert pd.isna(result.loc[0, "solar_forecast"])


@pytest.mark.parametrize("empty", [True, False])
def test_combined_summary_empty_months_totals_lifetime_and_query_count(summary_db, monkeypatch, empty):
    path, engine = summary_db
    now = datetime(2026, 3, 1, 12, tzinfo=ZoneInfo("Europe/Dublin"))
    freeze(monkeypatch, now)
    if not empty:
        with Session(engine) as session, session.begin():
            for stamp, value in [("2024-02-29T12:00:00+00:00", 10), ("2026-02-28T12:00:00+00:00", 3), (now.isoformat(), 2)]:
                session.add(SigenStorModbusSample(collected_at_utc=stamp))
                session.add(SigenStorHourlySample(hour_start_utc=stamp, import_tariff_band="a", sample_count=1, plant_pv_total_kwh_period=value))
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        daily, monthly = data.load_recent_energy_summaries(path, now.tzinfo.key, now.date(), ARRAYS)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(daily) == 8
    assert len(monthly) == 14
    assert daily["period"].tolist() == ["2026-03-01", "2026-02-28", "2026-02-27", "2026-02-26", "2026-02-25", "2026-02-24", "2026-02-23", "Total"]
    assert monthly["period"].tolist() == ["Mar 2026", "Feb 2026", "Jan 2026", "Dec 2025", "Nov 2025", "Oct 2025", "Sep 2025", "Aug 2025", "Jul 2025", "Jun 2025", "May 2025", "Apr 2025", "Total", "Lifetime"]
    assert monthly.iloc[2:12][list(data.ENERGY_SUMMARY_COLUMNS)].isna().all().all()
    if empty:
        assert monthly[list(data.ENERGY_SUMMARY_COLUMNS)].isna().all().all()
        assert daily[list(data.ENERGY_SUMMARY_COLUMNS)].isna().all().all()
    else:
        assert daily.iloc[-1]["solar_actual"] == 5
        assert monthly.iloc[-2]["solar_actual"] == 5
        assert monthly.iloc[-1]["solar_actual"] == 15
    queries = [sql for sql in statements if "WITH calendar" in sql]
    assert len(statements) == 3  # Two indexed lifetime minima and one aggregation.
    assert len(queries) == 1
    assert "LAG(cumulative)" in queries[0]
    pd.testing.assert_frame_equal(data.load_recent_daily_energy(path, now.tzinfo.key, now.date(), 7, ARRAYS), daily)
    pd.testing.assert_frame_equal(data.load_recent_monthly_energy(path, now.tzinfo.key, now.date(), 12, ARRAYS), monthly)


def test_snapshot_shares_the_two_summary_tables(monkeypatch):
    daily, monthly = pd.DataFrame(), pd.DataFrame()
    calls = []
    def summaries(*args):
        calls.append(args)
        return daily, monthly
    monkeypatch.setattr(data, "load_recent_energy_summaries", summaries)
    monkeypatch.setattr(data, "load_day", lambda *args: pd.DataFrame())
    monkeypatch.setattr(data, "load_latest_collection_local", lambda *args: None)
    config = SimpleNamespace(timezone="UTC", database=SimpleNamespace(path="unused"),
        forecast=SimpleNamespace(arrays=ARRAYS), dashboard=SimpleNamespace(actuals_to_forecast={}))
    result = snapshot.load_recent_data(config, SimpleNamespace(snapshot=lambda: None))
    assert len(calls) == 1
    assert result[3] is daily
    assert result[4] is monthly


@pytest.mark.parametrize("days,months", [(0, 12), (7, 0), (-1, 12)])
def test_invalid_summary_periods_are_rejected(summary_db, days, months):
    with pytest.raises(ValueError, match="at least one"):
        data.load_recent_energy_summaries(summary_db[0], "UTC", date(2026, 1, 1), ARRAYS, days, months)


def test_reversed_daily_range_is_rejected(summary_db):
    with pytest.raises(ValueError, match="End date"):
        data.load_daily_energy_totals(summary_db[0], "UTC", date(2026, 2, 1), date(2026, 1, 1), ARRAYS)
