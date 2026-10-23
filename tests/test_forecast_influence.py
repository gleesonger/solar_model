from datetime import date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from config import ForecastModelConfig

from forecast import evaluate_load_profile as load, operational_model as solar


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
