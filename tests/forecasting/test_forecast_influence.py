from datetime import date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from solar_model.config import ForecastModelConfig

import solar_model.tools.evaluate_load as load
import solar_model.planning.forecasts.solar as solar
from solar_model.planning.forecasts.house_load import history_start_day


def load_history():
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-10-22T00:00Z", "2026-10-18T00:00Z"]),
        "day": [date(2026, 10, 22), date(2026, 10, 18)],
        "hour": [0, 0], "day_type": ["weekday", "weekend"], "load_kwh": [2., 8.],
    })


@pytest.mark.parametrize("fraction,expected", [(0.2, 118 / 51), (0.5, 38 / 17), (1., 2.)])
def test_load_fraction_controls_profile_influence(fraction, expected):
    # With a one-day half-life, weekday evidence is 1/2 and weekend evidence
    # is 1/32. Their all-days mean is 40/17 kWh.
    settings = ForecastModelConfig(42., 1., fraction)
    result = load.predict_day(load_history(), date(2026, 10, 23), ZoneInfo("UTC"), settings)
    assert result.loc[0, "prediction_kwh"] == pytest.approx(expected)


@pytest.mark.parametrize("fraction", [0.2, 1.])
def test_load_without_matching_day_type_uses_all_days_mean(fraction):
    settings = ForecastModelConfig(42., 14., fraction)
    history = load_history().iloc[:1]
    result = load.predict_day(history, date(2026, 10, 24), ZoneInfo("UTC"), settings)
    assert result.loc[0, "type_weight"] == 0
    assert result.loc[0, "prediction_kwh"] == 2.
    assert pd.isna(result.loc[1, "prediction_kwh"])


def test_normal_profile_excludes_holiday_observations():
    history = load_history().iloc[:1].copy()
    holiday = history.copy()
    holiday["timestamp"] = pd.Timestamp("2026-10-21T00:00Z")
    holiday["day"] = date(2026, 10, 21)
    holiday["day_type"] = "holiday"
    holiday["load_kwh"] = 100.
    result = load.predict_day(pd.concat([history, holiday]), date(2026, 10, 23), ZoneInfo("UTC"), ForecastModelConfig(42., 14., 1.))
    assert result.loc[0, "prediction_kwh"] == 2.


def test_holiday_baseline_is_lowest_of_30_hourly_means_and_is_flat():
    target = date(2026, 10, 23)
    history = pd.DataFrame([
        {"timestamp": pd.Timestamp(day.isoformat(), tz="UTC") + pd.Timedelta(hours=hour),
         "day": day, "hour": hour, "day_type": "weekday", "load_kwh": 2. if hour == 3 else 5.}
        for day in (target - timedelta(days=offset) for offset in range(1, 31))
        for hour in range(24)
    ])
    result = load.predict_day(history, target, ZoneInfo("UTC"), ForecastModelConfig(42., 14., .2), {target})
    assert result.prediction_kwh.tolist() == [2.] * 24


def test_holiday_lookback_counts_holiday_dates_even_a_year_old():
    target = date(2026, 10, 23)
    days = [target - timedelta(days=30 * index) for index in range(1, 23)]
    periods = [(day.isoformat(), day.isoformat()) for day in days]
    assert history_start_day(periods, target, ForecastModelConfig(42., 14., 1.)) == days[20]
    history = pd.DataFrame([
        {"timestamp": pd.Timestamp(day.isoformat(), tz="UTC"), "day": day,
         "hour": 0, "day_type": "holiday", "load_kwh": 100. if index == 21 else 2.}
        for index, day in enumerate(days)
    ])
    result = load.predict_day(history, target, ZoneInfo("UTC"), ForecastModelConfig(42., 14., 1.), set(days + [target]))
    assert result.loc[0, "prediction_kwh"] == pytest.approx(2.)


@pytest.mark.parametrize("fraction", [0.2, 1 / 3, 0.3333, 0.5, 1.])
def test_solar_fraction_is_influence_after_one_effective_observation(fraction):
    settings = ForecastModelConfig(None, 1., fraction)
    issued = pd.DataFrame([{
        "target_hour": pd.Timestamp("2026-10-23T09:00Z"),
        "raw_total_kw": 2., "plant_pv_power_kw": 3., "baseline_kw": 2.,
    }])
    score, strength, count = solar._today_residual(
        issued, {9: (2., 1.)}, pd.Timestamp("2026-10-23T10:00Z"), settings,
    )
    assert (score, count) == (1., 1)
    assert strength == pytest.approx(fraction)


@pytest.mark.parametrize("fraction", [0., -1., 1.01, 20., np.inf, np.nan])
def test_invalid_fraction_rejected(fraction):
    with pytest.raises(ValueError, match="fraction"):
        ForecastModelConfig(42., 14., fraction)
