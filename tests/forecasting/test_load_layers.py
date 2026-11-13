"""Shared hierarchical load weighting, holiday isolation and sparse-hour fallbacks."""
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from solar_model.config import ForecastModelConfig
from solar_model.planning.forecasts.house_load import LoadObservation, blend_observations, predict_day


def history(rows: list[tuple[str, int, float, bool]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"timestamp": pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=hour), "day": date.fromisoformat(day), "hour": hour,
         "day_type": "holiday" if holiday else ("weekday" if date.fromisoformat(day).weekday() < 5 else "weekend"), "load_kwh": value}
        for day, hour, value, holiday in rows
    ])


def test_generic_blender_collapses_layers_in_order_and_retains_fractional_strength() -> None:
    target = pd.Timestamp("2026-11-16T00:00Z")
    layers = [[LoadObservation(target - pd.Timedelta(days=1), value)] for value in (2., 5., 11.)]
    result, evidence = blend_observations(layers, target, ForecastModelConfig(31., 1., .5))
    assert evidence == [.5, .5, .5]
    assert result == pytest.approx(17 / 3)
    # One shared influence controls every blend; at 1 the deepest usable layer wins.
    assert blend_observations(layers, target, ForecastModelConfig(31., 1., 1.))[0] == 11.


def test_empty_layers_skip_without_zero_filling_and_zero_load_is_valid() -> None:
    target = pd.Timestamp("2026-11-16T00:00Z")
    settings = ForecastModelConfig(31., 7., 1.)
    reading = LoadObservation(target - pd.Timedelta(days=1), 0.)
    assert blend_observations([[], [reading], []], target, settings)[0] == 0.
    assert pd.isna(blend_observations([[], [], []], target, settings)[0])


@pytest.mark.parametrize("holiday", [False, True])
def test_normal_and_holiday_streams_use_identical_three_layer_calculation(holiday: bool) -> None:
    target = date(2026, 11, 16)  # Monday
    rows = [("2026-11-09", 0, 9., holiday), ("2026-11-10", 0, 3., holiday), ("2026-11-15", 0, 1., holiday)]
    # Opposite stream must not affect any layer, including its all-days baseline.
    rows += [("2026-11-02", 0, 999., not holiday)]
    observed = history(rows)
    holidays = {target} if holiday else set()
    settings = ForecastModelConfig(31., 7., .5)
    result = predict_day(observed, target, ZoneInfo("UTC"), settings, holidays)
    monday, tuesday, sunday = .5, 2 ** (-6 / 7), 2 ** (-1 / 7)
    broad = (9 * monday + 3 * tuesday + sunday) / (monday + tuesday + sunday)
    weekday = (9 * monday + 3 * tuesday) / (monday + tuesday)
    intermediate = (broad + (monday + tuesday) * weekday) / (1 + monday + tuesday)
    expected = (intermediate + monday * 9) / (1 + monday)
    assert result.prediction_kwh.iloc[0] == pytest.approx(expected)
    assert result.type_weight.iloc[0] == pytest.approx(monday + tuesday)
    assert result.prediction_kwh.iloc[1:].isna().all()
    specific = predict_day(observed, target, ZoneInfo("UTC"), ForecastModelConfig(31., 7., 1.), holidays)
    assert specific.prediction_kwh.iloc[0] == 9.


def test_holiday_fallback_is_per_hour_and_never_used_when_holiday_evidence_exists() -> None:
    target = date(2026, 11, 16)
    observed = history([
        ("2026-11-09", 0, 2., True), ("2026-11-09", 1, float("nan"), True),
        ("2026-11-10", 0, 100., False), ("2026-11-10", 1, 5., False),
        ("2026-11-10", 2, 0., False),
    ])
    result = predict_day(observed, target, ZoneInfo("UTC"), ForecastModelConfig(31., 7., .2), {target})
    assert result.prediction_kwh.iloc[0] == pytest.approx(2.)
    assert result.prediction_kwh.iloc[1] == pytest.approx(5.)
    assert result.prediction_kwh.iloc[2] == 0.
    assert pd.isna(result.prediction_kwh.iloc[3])


def test_lookback_recency_and_forecast_cutoff_apply_to_every_layer() -> None:
    target = pd.Timestamp("2026-11-16T00:00Z")
    layers = [[
        LoadObservation(target - pd.Timedelta(days=2), 2.),
        LoadObservation(target - pd.Timedelta(days=1), 8.),
        LoadObservation(target - pd.Timedelta(days=3), 999.),
        LoadObservation(target, 999.),
        LoadObservation(target + pd.Timedelta(days=1), 999.),
        LoadObservation(target - pd.Timedelta(hours=1), float("nan")),
    ]] * 3
    result, evidence = blend_observations(layers, target, ForecastModelConfig(2., 1., .2))
    assert result == pytest.approx(6.)  # (2*0.25 + 8*0.5) / 0.75
    assert evidence == [.75, .75, .75]


def test_day_of_week_uses_local_date_instead_of_utc_weekday() -> None:
    zone = ZoneInfo("Pacific/Auckland")
    # UTC Sunday is local Monday; another local weekday has a different load.
    observed = history([("2026-11-08", 11, 9., False), ("2026-11-09", 11, 3., False)])
    local = observed.timestamp.dt.tz_convert(zone)
    observed["day"] = local.dt.date
    observed["hour"] = local.dt.hour
    result = predict_day(observed, date(2026, 11, 16), zone, ForecastModelConfig(31., 7., 1.))
    assert result.prediction_kwh.iloc[0] == 9.


def test_normal_lookback_counts_non_holiday_dates_not_calendar_days_or_matching_weekdays() -> None:
    from solar_model.planning.forecasts.house_load import normal_history_start_day, history_start_day
    target = date(2026, 11, 16)
    holidays = {date(2026, 11, 14), date(2026, 11, 15)}
    assert normal_history_start_day(target, 3., holidays) == date(2026, 11, 11)
    assert history_start_day(target, ForecastModelConfig(3., 3., 1.), [("2026-11-14", "2026-11-15")]) == date(2026, 11, 10)
    observed = history([
        ("2026-11-10", 0, 999., False),  # Fourth normal day: excluded.
        ("2026-11-11", 0, 2., False), ("2026-11-12", 0, 2., False), ("2026-11-13", 0, 2., False),
        ("2026-11-14", 0, 999., True), ("2026-11-15", 0, 999., True),
    ])
    result = predict_day(observed, target, ZoneInfo("UTC"), ForecastModelConfig(3., 3., 1.), holidays)
    assert result.prediction_kwh.iloc[0] == pytest.approx(2.)
    assert pd.isna(result.prediction_kwh.iloc[1])


def test_normal_window_uses_local_dates_across_dst_and_keeps_calendar_decay() -> None:
    from solar_model.planning.forecasts.house_load import normal_history_start_day
    target = date(2026, 10, 27)
    holidays = {date(2026, 10, 25), date(2026, 10, 26)}
    assert normal_history_start_day(target, 2., holidays) == date(2026, 10, 23)
    observed = history([("2026-10-23", 12, 2., False), ("2026-10-24", 12, 8., False)])
    result = predict_day(observed, target, ZoneInfo("Europe/Dublin"), ForecastModelConfig(2., 1., .2), holidays)
    ages = (pd.Timestamp("2026-10-27T00:00Z") - observed.timestamp).dt.total_seconds() / 86400
    weights = 2 ** (-ages)
    broad = (observed.load_kwh * weights).sum() / weights.sum()
    friday_weight = weights.iloc[0]
    expected = (broad + (friday_weight / 4) * 2.) / (1 + friday_weight / 4)
    assert result.prediction_kwh.iloc[12] == pytest.approx(expected)


def test_holiday_fallback_uses_full_non_holiday_window() -> None:
    target = date(2026, 11, 16)
    holidays = {date(2026, 11, 14), date(2026, 11, 15), target}
    observed = history([("2026-11-13", 0, 4., False), ("2026-11-14", 1, 100., True), ("2026-11-15", 1, 100., True)])
    result = predict_day(observed, target, ZoneInfo("UTC"), ForecastModelConfig(1., 3., .2), holidays)
    assert result.prediction_kwh.iloc[0] == 4.
    assert result.prediction_kwh.iloc[1] == 100.
