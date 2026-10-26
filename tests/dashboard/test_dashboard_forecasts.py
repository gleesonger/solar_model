from solar_model.dashboard import formatting
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from solar_model.config import SolarArrayConfig
from solar_model.storage.queries.types import SUMMARY_LATEST_KEYS
import solar_model.dashboard.components.charts as charts
import solar_model.storage.queries.dashboard as data


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

    assert adjusted.loc[adjusted["hour"] == "01:00", "forecast_total"].item() == 2.0
    assert raw.loc[raw["hour"] == "01:00", "forecast_total"].item() == 1.0


def test_aggregate_forecast_rejects_an_implicit_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown forecast source"):
        data.aggregate_forecast(
            [],
            datetime(2026, 9, 15, tzinfo=ZoneInfo("UTC")),
            (ARRAY,),
            source="other",
        )


def test_custom_chart_preview_splits_each_selected_metric_independently() -> None:
    frames = [
        pd.DataFrame({
            "date": ["2026-01-15", "2026-01-15"],
            "hour": ["00:00", "01:00"],
            "solar": [1.26, 10.0], "load": [2.0, 20.0],
        }),
        pd.DataFrame({
            "date": ["2026-02-15", "2026-02-15"],
            "hour": ["00:00", "01:00"],
            "solar": [3.0, 30.0], "load": [4.0, 40.0],
        }),
    ]

    preview = data.custom_chart_preview_from_frames(
        frames,
        datetime(2026, 1, 1),
        datetime(2026, 12, 31, 23, 59),
        "hour",
        "average",
        (("solar", "month"), ("load", None)),
    )

    assert preview["periods"] == ["00:00", "01:00"]
    assert preview["series"] == [
        {"metric": "solar", "split": "Jan", "data": [1.3, 10.0]},
        {"metric": "solar", "split": "Feb", "data": [3.0, 30.0]},
        {"metric": "load", "split": None, "data": [3.0, 30.0]},
    ]
    options = charts.custom_chart_preview_options(preview, {"solar": "Solar", "load": "Load"})
    assert options["yAxis"]["name"] == "kWh"
    assert "legend" not in options
    filtered = charts.custom_chart_preview_options(
        preview, {"solar": "Solar", "load": "Load"},
        visible_metrics={"solar", "load"}, visible_splits={"Jan"},
    )
    assert [series["name"] for series in filtered["series"]] == ["Solar (kWh) — Jan", "Load (kWh)"]


def test_custom_chart_treats_battery_as_a_level_not_an_energy_flow() -> None:
    frame = pd.DataFrame({
        "date": ["2026-01-15", "2026-01-16"],
        "hour": ["00:00", "00:00"],
        "battery": [4.0, 6.0],
    })

    preview = data.custom_chart_preview_from_frames(
        [frame], datetime(2026, 1, 1), datetime(2026, 1, 31, 23, 59),
        "hour", "sum", (("battery", None),),
    )

    assert preview["series"] == [{"metric": "battery", "split": None, "data": [5.0]}]


def test_custom_chart_recognises_per_orientation_solar_as_energy() -> None:
    assert charts.custom_chart_metric_unit("actual_panel_2") == "kWh"


@pytest.mark.parametrize("metric_count,split_count,color_dimension", [
    (2, 3, "splits"), (3, 2, "metrics"), (3, 3, "metrics"),
])
def test_custom_chart_styles_encode_dimensions_and_survive_filtering(
    metric_count: int, split_count: int, color_dimension: str,
) -> None:
    preview = {"periods": ["00:00", "01:00"], "series": [
        {"metric": f"metric_{metric}", "split": f"split_{split}", "data": [1, 2]}
        for metric in range(metric_count) for split in range(split_count)
    ]}
    assert charts.custom_chart_dimension_styles(preview["series"])["color_dimension"] == color_dimension
    full = charts.custom_chart_preview_options(preview, {})["series"]
    for metric in range(metric_count):
        for split in range(split_count):
            appearance = full[metric * split_count + split]
            color_index = metric if color_dimension == "metrics" else split
            style_index = split if color_dimension == "metrics" else metric
            assert appearance["lineStyle"]["color"] == charts.CUSTOM_CHART_COLORS[color_index]
            assert appearance["itemStyle"]["color"] == charts.CUSTOM_CHART_COLORS[color_index]
            assert appearance["lineStyle"]["type"] == ["solid", [5, 5], [2, 6]][style_index]
    # Filtering reverses which dimension has more visible entries, but not its encoding.
    filtered = charts.custom_chart_preview_options(
        preview, {}, visible_metrics={"metric_1"}, visible_splits={"split_1"},
    )["series"]
    assert filtered == [full[split_count + 1]]


def test_custom_chart_unsplit_lines_are_solid_and_keep_colours_when_filtered() -> None:
    preview = {"series": [
        {"metric": metric, "split": None, "data": [1, 2]}
        for metric in ("solar", "load", "battery")
    ]}
    full = charts.custom_chart_preview_options(preview, {})["series"]
    assert len({item["lineStyle"]["color"] for item in full}) == 3
    assert all(item["lineStyle"].get("type", "solid") == "solid" and not item["showSymbol"] for item in full)
    assert charts.custom_chart_preview_options(preview, {}, visible_metrics={"load"})["series"] == [full[1]]


def test_custom_chart_uses_agreed_style_order_then_automatic_defaults() -> None:
    import json
    import xml.etree.ElementTree as ET

    values = [None, *range(30), None]
    preview = {"series": [
        {"metric": f"metric_{metric}", "split": f"split_{split}", "data": values}
        for metric in range(13) for split in range(12)
    ]}
    styles = charts.custom_chart_dimension_styles(preview["series"])["splits"]
    assert [style["name"] for style in styles.values()] == [
        "Solid", "Short dash", "Dotted", "Filled circle", "Filled square",
        "Filled triangle", "Cross", "Empty circle", "Empty square",
        "Empty triangle", "Long dash", "Automatic",
    ]
    full = charts.custom_chart_preview_options(preview, {})["series"][:12]
    for index, (_, dash, symbol) in enumerate(charts.CUSTOM_CHART_STYLES):
        assert full[index]["lineStyle"]["type"] == dash
        assert full[index]["showSymbol"] == (symbol is not None)
        sample = ET.fromstring(charts.custom_chart_legend_sample(styles[f"split_{index}"]))
        assert len(sample) == (2 if symbol else 1)
        if symbol:
            callback = full[index][":symbol"]
            assert json.dumps(symbol) in callback
            indices = json.loads(callback.split("=> ", 1)[1].split(".includes", 1)[0])
            assert len(indices) <= 12
            assert indices[0] == 1 and indices[-1] == 30
            assert all(values[position] is not None for position in indices)
        if symbol and symbol.startswith("empty"):
            assert sample[1].attrib["fill"] == "white"
    assert "type" not in full[11]["lineStyle"]
    assert ":symbol" not in full[11]


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

    solar_row = next(row for row in formatting.summary_rows(frame, latest) if row["metric"] == "Solar")

    assert solar_row["forecast"] == "2.0"
    assert solar_row["forecast_raw"] == "1.0"


def test_summary_rows_exposes_live_forecast_blended_from_actuals_and_latest_forecast() -> None:
    frame = pd.DataFrame({
        "solar": [1.0, float("nan")],
        "battery": [0.0, 0.0],
        "load": [0.0, 0.0],
        "grid_import": [0.0, 0.0],
        "grid_export": [0.0, 0.0],
        "forecast_total": [3.0, 2.0],
        "forecast_bod_total": [4.0, 3.0],
        "forecast_raw_total": [5.0, 4.0],
        "latest_forecast_total": [1.0, 2.0],
    })
    latest = {key: 0.0 for key in SUMMARY_LATEST_KEYS.values()}

    solar_row = next(row for row in formatting.summary_rows(frame, latest) if row["metric"] == "Solar")

    assert solar_row["latest_forecast"] == "3.0"
    assert solar_row["forecast"] == "7.0"
    assert solar_row["forecast_raw"] == "9.0"


def test_recent_house_load_actuals_and_bod_forecast(tmp_path, monkeypatch):
    from solar_model.dashboard.tabs.recent import RecentTab
    from solar_model.storage.migrations import open_database
    from solar_model.storage.schema import ForecastHouseLoadSample, SigenStorHourlySample
    from solar_model.storage.queries.types import LivePowerData
    path = tmp_path / "house-recent.db"
    database = open_database(path)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 10, tzinfo=ZoneInfo("UTC")).astimezone(tz)
    monkeypatch.setattr(data, "datetime", Clock)
    try:
        with database.sessions.begin() as session:
            session.add(SigenStorHourlySample(hour_start_utc="2026-10-26T00:00Z", import_tariff_band="day",
                sample_count=1, house_load_kwh_period=.337, plant_load_total_kwh_period=.7))
            for hour in range(24):
                session.add(ForecastHouseLoadSample(target_day="2026-10-26",
                    forecast_time=f"2026-10-26T{hour:02d}:00Z", collected_at_utc="2026-10-26T00:00Z", load_kwh=1.))
        frame = data.load_day(str(path), "UTC", (), {})
        live = LivePowerData("2026-10-26T10:00Z", "2026-10-26T10:00Z", {"load": .35})
        rows = formatting.summary_rows(frame, RecentTab._live_table_values(live))
        house = next(row for row in rows if row["metric"] == "House Load")
        assert (house["latest"], house["today"], house["forecast"]) == ("0.3", "0.3", "24.0")
        assert house["latest_forecast"] == house["forecast_raw"] == ""
        daily, monthly = data.load_recent_energy_summaries(str(path), "UTC", date(2026, 10, 26), ())
        assert daily.loc[daily.period == "2026-10-26", "house_load"].iloc[0] == pytest.approx(.337)
        assert monthly.loc[monthly.period == "Oct 2026", "house_load"].iloc[0] == pytest.approx(.337)
        assert "house_load_forecast" not in daily and "house_load_forecast" not in monthly
        with database.sessions.begin() as session:
            session.query(ForecastHouseLoadSample).delete()
        missing = formatting.summary_rows(data.load_day(str(path), "UTC", (), {}), RecentTab._live_table_values(live))
        assert next(row for row in missing if row["metric"] == "House Load")["forecast"] == ""
    finally:
        database.close()
