"""As-of-safe actual-plus-adjusted solar profiles for the hidden planning day."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from solar_model.config import load_config
from solar_model.planning.scenarios import create_scenarios
from solar_model.planning.types import ForecastData, ForecastInterval
from solar_model.storage.migrations import open_database
from solar_model.storage.queries import planning
from solar_model.storage.schema import ForecastSolarSample, SigenStorModbusSample


def test_hidden_solar_repeats_actuals_and_adjusted_forecast_without_future_leakage(tmp_path, monkeypatch):
    database = open_database(tmp_path / "solar-profile.db")
    config = load_config()
    config = replace(config, timezone="UTC", forecast=replace(config.forecast,
        solar=replace(config.forecast.solar, arrays=(config.forecast.solar.arrays[0],))))
    panel = config.forecast.solar.arrays[0].panel_id
    day = datetime(2026, 10, 28, tzinfo=timezone.utc)
    as_of = day + timedelta(hours=12, minutes=30)
    try:
        with database.sessions.begin() as session:
            for hour in range(1, 24):
                session.add(ForecastSolarSample(collected_at_utc=as_of - timedelta(minutes=1),
                    forecast_time=day + timedelta(hours=hour), **{
                        f"panel_{panel}_adj_watt_hours": hour * 1000.,
                        f"panel_{panel}_raw_watt_hours": hour * 5000.}))
            for stamp, power in [(day + timedelta(hours=8, minutes=15), 3.),
                                 (day + timedelta(hours=12, minutes=15), 4.),
                                 (as_of + timedelta(minutes=1), 100.)]:
                session.add(SigenStorModbusSample(collected_at_utc=stamp, plant_pv_power_kw=power))
            session.add(ForecastSolarSample(collected_at_utc=as_of + timedelta(minutes=1),
                forecast_time=day + timedelta(hours=14), **{f"panel_{panel}_adj_watt_hours": 100000.}))
        start, end = planning._forecast_bounds(as_of, config)
        monkeypatch.setattr(planning, "load_house_load_intervals", lambda *args: [ForecastInterval(start, end, 0.)])
        forecast = planning.load_forecast_as_of(database, as_of, config)
        def energy(left):
            return ForecastData._sum_intervals(forecast.solar, left, left + timedelta(hours=1))
        # Visible forecast is retained; hidden morning uses measured solar.
        assert energy(day + timedelta(hours=13)) == pytest.approx(1.)
        assert energy(day + timedelta(days=2, hours=8)) == pytest.approx(3.)
        # Today's elapsed half hour: 4 kW * .5h; remaining half hour: 1 kW * .5h.
        assert energy(day + timedelta(days=2, hours=12)) == pytest.approx(2.5)
        assert energy(day + timedelta(days=1, hours=14)) == pytest.approx(1.)
        # Missing actuals use the saved adjusted forecast, not its 5x raw values.
        assert energy(day + timedelta(days=2, hours=9)) == pytest.approx(1.)
        scenarios = create_scenarios(forecast)
        low = next(s for s in scenarios if s.name == "base_low")
        assert low.forecast_data.sum(day + timedelta(days=2, hours=8),
            day + timedelta(days=2, hours=9)).solar_kwh == pytest.approx(1.5)
    finally:
        database.close()


@pytest.mark.parametrize("day", [datetime(2026, 3, 29, tzinfo=timezone.utc), datetime(2026, 10, 25, tzinfo=timezone.utc)])
def test_repeated_profile_remains_contiguous_across_dst(tmp_path, day):
    database = open_database(tmp_path / "dst-profile.db")
    config = replace(load_config(), timezone="Europe/Dublin")
    as_of = day + timedelta(hours=3, minutes=5)
    start, end = planning._forecast_bounds(as_of, config)
    source_start = day - timedelta(hours=2)
    solar = [ForecastInterval(source_start, end, (end - source_start).total_seconds() / 3600)]
    try:
        result = planning._lookahead_solar(database, solar, as_of, config, start, end)
        assert result[0].starts_at == start
        assert result[-1].ends_at == end
        assert all(a.ends_at == b.starts_at for a, b in zip(result, result[1:]))
        assert all(i.ends_at > i.starts_at for i in result)
        hidden_start = as_of + timedelta(hours=24)
        assert ForecastData._sum_intervals(tuple(result), hidden_start, hidden_start + timedelta(hours=24)) > 0
    finally:
        database.close()
