from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config import SolarArrayConfig
from dashboard import data


ARRAY = SolarArrayConfig(
    panel_id=1,
    name="Roof",
    latitude=0,
    longitude=0,
    declination=0,
    azimuth=0,
    peak_kw=1,
)


def _forecast_row(*, raw: float, adjusted: float) -> SimpleNamespace:
    return SimpleNamespace(
        forecast_time="2026-09-15T01:00:00+00:00",
        panel_1_raw_watt_hours=raw,
        panel_1_adj_watt_hours=adjusted,
    )


def test_aggregate_forecast_selects_requested_raw_or_adjusted_series() -> None:
    row = _forecast_row(raw=1_000, adjusted=2_000)
    day_start = datetime(2026, 9, 15, tzinfo=ZoneInfo("UTC"))

    adjusted = data.aggregate_forecast([row], day_start, (ARRAY,), source="adjusted")
    raw = data.aggregate_forecast([row], day_start, (ARRAY,), source="raw")

    assert adjusted.loc[adjusted["hour"] == "00:00", "forecast_total"].item() == 2.0
    assert raw.loc[raw["hour"] == "00:00", "forecast_total"].item() == 1.0


def test_aggregate_forecast_rejects_an_implicit_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown forecast source"):
        data.aggregate_forecast(
            [],
            datetime(2026, 9, 15, tzinfo=ZoneInfo("UTC")),
            (ARRAY,),
            source="other",
        )


def test_summary_rows_exposes_adjusted_forecast_and_raw_forecast_separately() -> None:
    frame = pd.DataFrame({
        "solar": [0.0],
        "battery": [0.0],
        "load": [0.0],
        "grid_import": [0.0],
        "grid_export": [0.0],
        "forecast_total": [2.0],
        "forecast_raw_total": [1.0],
    })
    latest = {
        "solar": 0.0,
        "battery": 0.0,
        "inverter": 0.0,
        "load": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
    }

    solar_row = next(row for row in data.summary_rows(frame, latest) if row["metric"] == "Solar")

    assert solar_row["forecast"] == "2.0"
    assert solar_row["forecast_raw"] == "1.0"
