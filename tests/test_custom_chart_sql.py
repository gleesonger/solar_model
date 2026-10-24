from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine, delete, event
from sqlalchemy.orm import Session

from config import SolarArrayConfig
from dashboard import data
from database import HOURLY_VALID_COUNT_COLUMNS, ForecastLoadSample, ForecastSolarSample, SigenStorHourlySample, SigenStorModbusSample


def _hourly_sample(**values):
    counts = {counter: values.get("sample_count", 0) if values.get(field) is not None else 0
              for field, counter in HOURLY_VALID_COUNT_COLUMNS.items()}
    return SigenStorHourlySample(**counts, **values)


ARRAYS = (
    SolarArrayConfig(panel_id=1, name="Roof", latitude=0, longitude=0, declination=0, azimuth=0, peak_kw=1),
    SolarArrayConfig(panel_id=2, name="Garden", latitude=0, longitude=0, declination=0, azimuth=0, peak_kw=1),
)
MAPPING = {"pv1": 1, "pv2": 2}


def legacy_preview(path, zone, bounds, group, aggregation, arrays, mapping, selections):
    """The previous pandas pipeline, used as an independent regression reference."""
    battery_requested = any(metric == "battery" for metric, _ in selections)
    source = data.load_analysis_source_data(path, bounds, include_minute_samples=battery_requested)
    frames = data.load_rollup_daily_energy_frames(
        path, zone, bounds.start_date, bounds.end_date, arrays, mapping, source.forecast_rows,
    )
    if battery_requested:
        battery_frame = pd.DataFrame([{
            "date": data.parse_time(sample.collected_at_utc, zone).date().isoformat(),
            "hour": data.parse_time(sample.collected_at_utc, zone).strftime("%H:00"),
            "battery": sample.plant_battery_available_discharge_kwh,
        } for sample in source.samples])
        if not battery_frame.empty:
            hourly = battery_frame.groupby(["date", "hour"], as_index=False)["battery"].mean()
            energy = pd.concat(frames, ignore_index=True).drop(columns="battery", errors="ignore")
            frames = [energy.merge(hourly, on=["date", "hour"], how="outer")]
    return data.custom_chart_preview_from_frames(
        frames, bounds.start_at, bounds.end_at, group, aggregation, selections,
    )


def legacy_analysis(path, zone, bounds, group, aggregation, arrays=ARRAYS, mapping=MAPPING):
    single_day = bounds.start_date == bounds.end_date
    source = data.load_analysis_source_data(path, bounds, include_minute_samples=single_day)
    frames = data.load_rollup_daily_energy_frames(
        path, zone, bounds.start_date, bounds.end_date, arrays, mapping, source.forecast_rows,
    )
    energy = data.historical_energy_from_frames(frames, group, bounds.start_at, bounds.end_at, aggregation=aggregation)
    if single_day:
        power, battery = data.average_telemetry_by_interval(source.samples, zone, 1, mapping)
    else:
        power, battery = data.average_hourly_rollup_telemetry(data.load_analysis_hourly_samples(path, bounds), zone, mapping)
    return data.AnalysisRangeData(energy=energy, power=power, battery=battery)


def assert_analysis_matches(actual, expected):
    for name in ("energy", "power", "battery"):
        actual_frame = getattr(actual, name)
        expected_frame = getattr(expected, name).copy()
        if "load_forecast" in actual_frame and "load_forecast" not in expected_frame:
            expected_frame["load_forecast"] = float("nan")
        for column in expected_frame:
            if column not in {"period", "time"}:
                expected_frame[column] = pd.to_numeric(expected_frame[column], errors="coerce").astype("float64")
        pd.testing.assert_frame_equal(actual_frame, expected_frame, check_like=True, rtol=1e-12, atol=1e-12)


@pytest.fixture
def chart_database(tmp_path, request):
    path = str(tmp_path / "chart.db")
    engine = create_engine(f"sqlite:///{path}")
    models = (ForecastLoadSample, ForecastSolarSample, SigenStorHourlySample, SigenStorModbusSample)
    ForecastSolarSample.__table__.metadata.create_all(engine, tables=[model.__table__ for model in models])
    timezone_name, start = getattr(request, "param", ("Europe/Dublin", date(2026, 10, 24)))
    zone = ZoneInfo(timezone_name)
    with Session(engine) as session:
        for day_offset in range(4):
            day = start + timedelta(days=day_offset)
            midnight = datetime.combine(day, time.min, zone)
            # The second collection must never replace the earliest daily one.
            if day_offset != 2:
                for collection_hour in (0, 5):
                    collection = midnight + timedelta(hours=collection_hour, minutes=15)
                    for forecast_hour, cumulative in ((0, 100), (1, 400), (2, 200), (3, 900), (12, 4000), (23, 8000), (24, 200)):
                        stamp = midnight + timedelta(hours=forecast_hour)
                        if datetime.fromtimestamp(stamp.timestamp(), zone).replace(tzinfo=None) != stamp.replace(tzinfo=None):
                            continue
                        session.add(ForecastSolarSample(
                            collected_at_utc=collection.isoformat(), forecast_time=stamp.isoformat(),
                            panel_1_raw_watt_hours=cumulative * (1 + collection_hour),
                            panel_1_adj_watt_hours=cumulative * 2 * (1 + collection_hour),
                            panel_2_raw_watt_hours=None if forecast_hour == 1 else cumulative / 2,
                            panel_2_adj_watt_hours=None if day_offset == 1 or forecast_hour == 1 else cumulative,
                        ))
            # Include a forecast-only day and a day containing no forecasts.
            if day_offset != 0:
                for hour in (0, 1, 2, 3, 12, 23):
                    stamp = midnight + timedelta(hours=hour)
                    if datetime.fromtimestamp(stamp.timestamp(), zone).replace(tzinfo=None) != stamp.replace(tzinfo=None):
                        continue
                    for tariff, count in (("night", 2), ("day", 3)):
                        session.add(_hourly_sample(
                            hour_start_utc=stamp.isoformat(), import_tariff_band=tariff, sample_count=count,
                            plant_pv_total_kwh_period=0.015 if hour == 0 else 2.1,
                            plant_load_total_kwh_period=3.2,
                            plant_grid_import_total_kwh_period=0.15,
                            plant_grid_export_total_kwh_period=None if hour == 3 else 1.2,
                            grid_import_cost_period=0.6, grid_export_revenue_period=0.1,
                            net_cost_period=0.5, no_solar_battery_import_cost_period=1.3,
                            import_rate=0.25 * count, export_rate=0.12 * count,
                            plant_battery_charge_total_kwh_period=0.7,
                            plant_battery_discharge_total_kwh_period=0.2,
                            inverter_pv1_energy_kwh_period=-1 if hour == 0 else 1,
                            inverter_pv2_energy_kwh_period=0 if hour == 0 else 2,
                            plant_pv_power_kw=2.3 * count,
                            plant_load_power_kw=3 * count,
                            plant_battery_power_kw=-0.8 * count,
                            plant_grid_power_kw=(1.5 if tariff == "day" else -0.4) * count,
                            grid_power_sample_count=count,
                            plant_grid_import_power_kw=1.5 * count if tariff == "day" else 0.,
                            plant_grid_export_power_kw=0.4 * count if tariff == "night" else 0.,
                            plant_battery_available_discharge_kwh=5.25 * count,
                            plant_battery_soc_percent=53 * count,
                            inverter_power_kw=2.1 * count,
                            inverter_pv1_power_kw=0.6 * count,
                            inverter_pv2_power_kw=0.9 * count,
                        ))
                    for minute in (10, 30):
                        session.add(SigenStorModbusSample(
                            collected_at_utc=(stamp + timedelta(minutes=minute)).isoformat(),
                            plant_battery_available_discharge_kwh=None if hour == 3 else 5 + minute / 60,
                            plant_pv_power_kw=(hour + 1) * 0.3 + minute / 300,
                            plant_load_power_kw=2.4,
                            plant_battery_power_kw=-0.5,
                            plant_grid_power_kw=2 if minute == 10 else -1,
                            plant_battery_soc_percent=50 + minute / 10,
                            inverter_power_kw=2.1,
                            inverter_pv1_power_kw=0.6,
                            inverter_pv2_power_kw=0.9,
                        ))

        # Both occurrences of the repeated local hour on the DST transition.
        if timezone_name == "Europe/Dublin" and start == date(2026, 10, 24):
            repeated = datetime(2026, 10, 25, 1, tzinfo=zone, fold=1)
            session.add(_hourly_sample(
                hour_start_utc=repeated.isoformat(), import_tariff_band="night", sample_count=1,
                plant_pv_total_kwh_period=0.8, plant_load_total_kwh_period=0.4,
                inverter_pv1_energy_kwh_period=0.1, inverter_pv2_energy_kwh_period=0.7,
            ))
        session.commit()
    bounds = data.analysis_date_bounds(start, start + timedelta(days=3), zone)
    try:
        yield path, zone, bounds
    finally:
        data.create_engine_for_database(path).dispose()
        engine.dispose()


@pytest.mark.parametrize("aggregation", ["sum", "average"])
@pytest.mark.parametrize("group,split", [
    ("hour", "month"), ("day", "hour"), ("week", "season"),
    ("month", "day"), ("season", "year"), ("year", "week"), ("hour", None),
])
def test_sql_preview_matches_daily_pandas_calculations(chart_database, aggregation, group, split):
    path, zone, bounds = chart_database
    metrics = [*data.CUSTOM_CHART_ENERGY_METRICS, "actual_panel_1", "actual_panel_2", "forecast_panel_1", "forecast_panel_2"]
    selections = tuple((metric, split) for metric in metrics)
    expected = legacy_preview(path, zone, bounds, group, aggregation, ARRAYS, MAPPING, selections)
    assert data.load_custom_chart_preview(
        path, str(zone), bounds, group, aggregation, ARRAYS, MAPPING, selections,
    ) == expected


@pytest.mark.parametrize("aggregation", ["sum", "average"])
def test_sql_preview_preserves_partial_hour_ranges_and_different_splits(chart_database, aggregation):
    path, zone, bounds = chart_database
    bounds = replace(bounds, start_at=bounds.start_at.replace(hour=1, minute=15), end_at=bounds.end_at.replace(hour=12, minute=10))
    selections = (("solar", "month"), ("load", None), ("forecast_raw_total", "year"), ("battery", None))
    assert data.load_custom_chart_preview(path, str(zone), bounds, "hour", aggregation, ARRAYS, MAPPING, selections) == legacy_preview(
        path, zone, bounds, "hour", aggregation, ARRAYS, MAPPING, selections,
    )


@pytest.mark.parametrize("metric", ["solar", "forecast_total", "forecast_raw_total", "forecast_panel_1", "actual_panel_1"])
def test_sql_reads_only_selected_measurements(chart_database, metric):
    path, zone, bounds = chart_database
    statements = []
    engine = data.create_engine_for_database(path)
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, ((metric, None),))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result["series"]
    assert len(statements) == 1
    sql = statements[0]
    assert "FROM sigenstor WHERE" not in sql
    if metric in {"solar", "actual_panel_1"}:
        assert "watt_hours" not in sql and "LAG(" not in sql
    if metric in {"solar", "forecast_total", "forecast_raw_total", "forecast_panel_1"}:
        assert "inverter_pv" not in sql
    if metric in {"forecast_total", "forecast_panel_1"}:
        assert "raw_watt_hours" not in sql
    if metric == "forecast_raw_total":
        assert "adj_watt_hours" not in sql
    if metric == "forecast_panel_1":
        assert "panel_2_adj_watt_hours" not in sql


def test_sql_preserves_empty_ranges_and_battery_fallback(chart_database):
    path, zone, bounds = chart_database
    bounds = data.analysis_date_bounds(date(2026, 10, 28), date(2026, 10, 28), zone)
    selections = (("solar", None), ("battery", None), ("forecast_total", None))
    expected = legacy_preview(path, zone, bounds, "hour", "sum", ARRAYS, MAPPING, selections)
    assert data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, selections) == expected


def test_sql_preserves_multiple_selections_of_the_same_variable(chart_database):
    path, zone, bounds = chart_database
    selections = (("solar", "month"), ("solar", None), ("solar", "day"), ("solar", "month"))
    assert data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, selections) == legacy_preview(
        path, zone, bounds, "hour", "sum", ARRAYS, MAPPING, selections,
    )


@pytest.mark.parametrize("chart_database", [
    ("Europe/Dublin", date(2026, 3, 28)),
    ("Asia/Kolkata", date(2026, 10, 24)),
    ("UTC", date(2026, 10, 24)),
], indirect=True)
@pytest.mark.parametrize("aggregation", ["sum", "average"])
def test_sql_preserves_spring_dst_and_configured_timezones(chart_database, aggregation):
    path, zone, bounds = chart_database
    selections = tuple((metric, "day") for metric in ("solar", "battery", "forecast_total", "forecast_raw_total", "forecast_panel_2"))
    assert data.load_custom_chart_preview(path, str(zone), bounds, "hour", aggregation, ARRAYS, MAPPING, selections) == legacy_preview(
        path, zone, bounds, "hour", aggregation, ARRAYS, MAPPING, selections,
    )


def test_sql_battery_uses_previous_fallback_when_no_minute_records_exist(chart_database):
    path, zone, bounds = chart_database
    with Session(data.create_engine_for_database(path)) as session:
        session.execute(delete(SigenStorModbusSample))
        session.commit()
    selections = (("battery", "month"),)
    assert data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, selections) == legacy_preview(
        path, zone, bounds, "hour", "sum", ARRAYS, MAPPING, selections,
    )


def test_sql_rejects_invalid_dimensions_before_querying(chart_database):
    path, zone, bounds = chart_database
    with pytest.raises(ValueError, match="Split by must be different"):
        data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, (("solar", "hour"),))


@pytest.mark.parametrize("aggregation", ["sum", "average"])
@pytest.mark.parametrize("group", ["hour", "day", "week", "month", "season", "year"])
def test_analysis_uses_shared_sql_and_preserves_full_precision(chart_database, aggregation, group):
    path, zone, bounds = chart_database
    expected = legacy_analysis(path, zone, bounds, group, aggregation)
    actual = data.load_analysis_range(path, str(zone), bounds, group, 60, ARRAYS, MAPPING, aggregation)
    assert_analysis_matches(actual, expected)
    assert set(column for column in actual.energy if column.startswith(data.IMPORT_COST_TARIFF_PREFIX)) == {
        "import_cost_tariff__day", "import_cost_tariff__night",
    }


@pytest.mark.parametrize("partial_range", [False, True])
@pytest.mark.parametrize("chart_database", [
    ("Europe/Dublin", date(2026, 10, 24)),
    ("Europe/Dublin", date(2026, 3, 28)),
    ("Asia/Kolkata", date(2026, 10, 24)),
], indirect=True)
def test_analysis_retains_minute_data_and_zoom_on_single_days(chart_database, partial_range):
    path, zone, all_bounds = chart_database
    day = all_bounds.start_date + timedelta(days=1)
    bounds = data.analysis_date_bounds(day, day, zone)
    if partial_range:
        bounds = replace(bounds, start_at=bounds.start_at.replace(hour=1, minute=15), end_at=bounds.end_at.replace(hour=12, minute=10))
    expected = legacy_analysis(path, zone, bounds, "hour", "sum")
    actual = data.load_analysis_range(path, str(zone), bounds, "hour", 60, ARRAYS, MAPPING)
    assert_analysis_matches(actual, expected)
    assert len(actual.power) == len(actual.battery) == 1440
    assert actual.power["time"].iloc[-1] == "23:59"


def test_analysis_and_custom_are_adapters_over_the_same_aggregation(chart_database, monkeypatch):
    path, zone, bounds = chart_database
    original = data.load_chart_data_batch
    requests = []
    def capture(*args, **kwargs):
        requests.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(data, "load_chart_data_batch", capture)
    def old_path(*args, **kwargs):
        pytest.fail("A chart used the old ORM/pandas data preparation path")
    for name in ("load_analysis_source_data", "load_rollup_daily_energy_frames", "average_telemetry_by_interval", "average_hourly_rollup_telemetry"):
        monkeypatch.setattr(data, name, old_path)
    analysis = data.load_analysis_range(path, str(zone), bounds, "hour", 60, ARRAYS, MAPPING)
    preview = data.load_custom_chart_preview(path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING, (("solar", None),))
    assert len(requests) == 2
    assert len(requests[0][0][5]) == 2
    assert all(split is None for args, kwargs in requests for request in args[5] for metric, split in request.selections)
    assert preview["series"][0]["data"] == data.chart_values(analysis.energy["solar"])
    assert analysis.energy["solar"].iloc[0] == pytest.approx(0.09)
    assert preview["series"][0]["data"][0] == 0.1


def test_tariff_names_are_bound_and_quoted_safely(chart_database):
    path, zone, bounds = chart_database
    name = 'peak \'A";--'
    with Session(data.create_engine_for_database(path)) as session:
        session.add(_hourly_sample(
            hour_start_utc=bounds.start_at.isoformat(), import_tariff_band=name, sample_count=1,
            grid_import_cost_period=0.1234,
        ))
        session.commit()
    expected = legacy_analysis(path, zone, bounds, "hour", "sum")
    result = data.load_analysis_range(path, str(zone), bounds, "hour", 60, ARRAYS, MAPPING)
    assert_analysis_matches(result, expected)
    assert result.energy[data.IMPORT_COST_TARIFF_PREFIX + name].iloc[0] == pytest.approx(0.1234)


@pytest.mark.parametrize("single_day", [False, True])
def test_complete_analysis_executes_one_statement_including_tariffs(chart_database, single_day):
    path, zone, bounds = chart_database
    if single_day:
        day = bounds.start_date + timedelta(days=1)
        bounds = data.analysis_date_bounds(day, day, zone)
    statements = []
    engine = data.create_engine_for_database(path)
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = data.load_analysis_range(path, str(zone), bounds, "hour", 60, ARRAYS, MAPPING)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    sql = statements[0]
    assert sql.startswith("WITH ")
    assert sql.count("FROM sigenstor_hourly ") == 1
    assert sql.count("FROM sigenstor ") == (1 if single_day else 0)
    assert "batch_forecasts AS MATERIALIZED" in sql
    assert "import_cost_tariff__day" in result.energy
    assert len(result.power) == (1440 if single_day else 24)


def test_batch_shares_sources_without_mixing_grouping_or_resolution(chart_database):
    path, zone, bounds = chart_database
    requests = (
        data.ChartRequest("daily", (("solar", None), ("load", None)), group_by="day"),
        data.ChartRequest("split 'chart'", (("solar", "month"),), aggregation="average"),
        data.ChartRequest("minute", (("power_solar", None), ("battery_level", None)), aggregation="average", sample_resolution="minute", interval_minutes=1),
        data.ChartRequest("tariffs", ((data.IMPORT_COST_TARIFF_PREFIX + "*", None),)),
    )
    expected = {
        request.name: data.load_chart_data(
            path, str(zone), bounds, request.group_by, request.aggregation, ARRAYS, MAPPING,
            request.selections, sample_resolution=request.sample_resolution, interval_minutes=request.interval_minutes,
        ) for request in requests
    }
    assert data.load_chart_data_batch(path, str(zone), bounds, ARRAYS, MAPPING, requests) == expected


def test_explicit_tariff_request_matches_analysis_and_unknown_band_is_skipped(chart_database):
    path, zone, bounds = chart_database
    analysis = data.load_analysis_range(path, str(zone), bounds, "hour", 60, ARRAYS, MAPPING)
    requested = data.load_chart_data(
        path, str(zone), bounds, "hour", "sum", ARRAYS, MAPPING,
        (("import_cost_tariff__day", None), ("import_cost_tariff__missing", None)),
    )
    assert len(requested["series"]) == 1
    assert requested["series"][0]["data"] == list(analysis.energy["import_cost_tariff__day"])


def test_empty_batches_skip_queries_and_request_names_are_unique(chart_database):
    path, zone, bounds = chart_database
    empty = data.ChartRequest("empty", ())
    assert data.load_chart_data_batch(path, str(zone), bounds, ARRAYS, MAPPING, (empty,)) == {
        "empty": {"periods": [], "series": []},
    }
    with pytest.raises(ValueError, match="unique"):
        data.load_chart_data_batch(path, str(zone), bounds, ARRAYS, MAPPING, (empty, empty))
