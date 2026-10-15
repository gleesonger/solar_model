from datetime import date, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from dashboard import data
from dashboard.download_data_dialog import excel_bytes, prepare_download
from database import ForecastSolarSample, SigenStorModbusSample


@pytest.fixture
def download_database(tmp_path):
    path = tmp_path / "download.db"
    engine = create_engine(f"sqlite:///{path}")
    SigenStorModbusSample.metadata.create_all(
        engine, tables=[SigenStorModbusSample.__table__, ForecastSolarSample.__table__]
    )
    zone = ZoneInfo("Europe/Dublin")
    with Session(engine) as session:
        for minute, band, energy, power, rate in (
            (5, "day", 0.1, 1.0, 0.4),
            (15, "day", 0.2, 2.0, 0.4),
            (25, "night", 0.3, 3.0, 0.2),
        ):
            session.add(SigenStorModbusSample(
                collected_at_utc=datetime(2026, 10, 14, 10, minute, tzinfo=zone).isoformat(),
                import_tariff_band=band,
                plant_load_total_kwh_period=energy,
                plant_load_power_kw=power,
                plant_load_total_kwh=100 + energy,
                import_rate=rate,
                grid_import_cost_period=energy * rate,
            ))
        session.add(ForecastSolarSample(
            collected_at_utc=datetime(2026, 10, 14, 0, 5, tzinfo=zone).isoformat(),
            forecast_time=datetime(2026, 10, 14, 10, tzinfo=zone).isoformat(),
            panel_1_raw_watts=2000,
            panel_1_raw_watt_hours=3000,
            panel_1_raw_watt_hours_day=10000,
        ))
        session.commit()
    engine.dispose()
    bounds = data.analysis_date_bounds(date(2026, 10, 14), date(2026, 10, 14), zone)
    return str(path), bounds, zone


@pytest.mark.parametrize("file_format,include_forecast,timestep", [
    ("CSV", False, "hour"),
    ("CSV", True, "day"),
    ("Excel", False, "hour"),
    ("Excel", True, "day"),
])
def test_analysis_download_aggregates_tariff_labels_and_measurements(
    download_database, file_format, include_forecast, timestep,
):
    path, bounds, zone = download_database
    frame = data.analysis_export_dataframe(
        path, bounds, timestep, zone, include_forecast=include_forecast,
    )
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["Import Tariff Band"] == "day / night"
    assert row["Load (kWh)"] == pytest.approx(0.6)
    assert row["Plant Load Power Kw"] == pytest.approx(2.0)
    assert row["Plant Load Total Kwh"] == pytest.approx(100.3)
    assert row["Import Rate"] == pytest.approx(1 / 3)
    assert row["Import Cost"] == pytest.approx(0.18)
    forecast_column = "Forecast Panel 1 Raw Watt Hours"
    assert (forecast_column in frame) == include_forecast
    if include_forecast:
        assert row[forecast_column] == pytest.approx(3000)

    content, filename, media_type = prepare_download(
        path, bounds, timestep, zone, include_forecast, file_format,
    )
    assert filename.startswith("analysis-data-2026-10-14-to-2026-10-14")
    if file_format == "CSV":
        exported = pd.read_csv(BytesIO(content))
        assert filename.endswith(".csv")
        assert media_type == "text/csv"
        assert exported.loc[0, "Import Tariff Band"] == "day / night"
        assert exported.loc[0, "Load (kWh)"] == pytest.approx(0.6)
    else:
        assert filename.endswith(".xlsx")
        assert media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        with ZipFile(BytesIO(content)) as workbook:
            assert workbook.testzip() is None
            assert "Analysis Data" in workbook.read("xl/workbook.xml").decode()
            assert "day / night" in workbook.read("xl/worksheets/sheet1.xml").decode()


@pytest.mark.parametrize("bands,expected", [
    (["day", "day"], "day"),
    ([None, "night", "day", "night"], "night / day"),
    ([None, None], None),
])
def test_export_tariff_labels_preserve_order_and_missing_values(bands, expected):
    period = datetime(2026, 10, 14, tzinfo=ZoneInfo("UTC"))
    records = [{"period": period, "import_tariff_band": band} for band in bands]
    frame = data.aggregate_analysis_export_records(records, columns=("import_tariff_band",))
    if expected is None:
        assert pd.isna(frame.loc[0, "Import Tariff Band"])
    else:
        assert frame.loc[0, "Import Tariff Band"] == expected


def legacy_export(path, bounds, timestep, zone, include_forecast):
    """Keep the previous ORM/pandas export as an independent regression reference."""
    source = data.load_analysis_source_data(path, bounds)
    actual = data.aggregate_analysis_export_records([
        {
            "period": data.analysis_interval_start(data.parse_time(sample.collected_at_utc, zone), timestep),
            **{column: getattr(sample, column) for column in data.SAMPLE_EXPORT_COLUMNS},
        }
        for sample in source.samples
    ], columns=data.SAMPLE_EXPORT_COLUMNS)
    if not include_forecast:
        return actual
    forecast = data.aggregate_analysis_export_records(
        data.analysis_forecast_export_records(source.forecast_rows, bounds, timestep, zone),
        columns=data.FORECAST_EXPORT_COLUMNS, forecast=True,
    )
    return actual.merge(forecast, on="period", how="outer").sort_values("period").reset_index(drop=True)


@pytest.fixture(params=[
    ("Europe/Dublin", date(2026, 3, 29)),
    ("Europe/Dublin", date(2026, 10, 25)),
    ("Asia/Kolkata", date(2026, 10, 14)),
])
def export_history(tmp_path, request):
    zone_name, day = request.param
    zone = ZoneInfo(zone_name)
    path = str(tmp_path / "history.db")
    engine = create_engine(f"sqlite:///{path}")
    SigenStorModbusSample.metadata.create_all(
        engine, tables=[SigenStorModbusSample.__table__, ForecastSolarSample.__table__]
    )
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(ZoneInfo("UTC"))
    with Session(engine) as session:
        # UTC stepping includes both occurrences of the autumn repeated hour.
        for index in range(100):
            stamp = start + timedelta(minutes=30 * index)
            session.add(SigenStorModbusSample(
                collected_at_utc=stamp.isoformat(),
                import_tariff_band=None if index % 7 == 0 else ("day" if index % 3 else "night"),
                plant_load_total_kwh_period=None if index % 5 == 0 else 0.0001 * index,
                plant_load_total_kwh=None if index % 4 == 0 else 200 - index / 100,
                plant_pv_total_kwh=0.0,
                plant_load_power_kw=None if index % 2 else index / 3,
                import_rate=0.2 if index % 3 else 0.4,
            ))
        for day_offset in range(3):
            midnight = datetime.combine(day + timedelta(days=day_offset), datetime.min.time(), zone)
            for issue_hour in (0, 3):
                for target_hour in (0, 1, 2, 12, 24):
                    stamp = midnight.astimezone(ZoneInfo("UTC")) + timedelta(hours=target_hour)
                    session.add(ForecastSolarSample(
                        collected_at_utc=(midnight + timedelta(hours=issue_hour, minutes=5)).isoformat(),
                        forecast_time=stamp.isoformat(),
                        panel_1_raw_watts=10 * (1 + target_hour + issue_hour),
                        panel_1_raw_watt_hours=100 - target_hour - issue_hour,
                        panel_1_raw_watt_hours_day=50 - target_hour,
                        panel_1_adj_watts=None if target_hour == 12 else 0.0,
                    ))
        session.commit()
    engine.dispose()
    bounds = data.analysis_date_bounds(day, day + timedelta(days=1), zone)
    return path, bounds, zone


@pytest.mark.parametrize("timestep", ["minute", "hour", "day", "week", "month", "season", "year"])
@pytest.mark.parametrize("include_forecast", [False, True])
def test_sql_export_matches_previous_pipeline(export_history, timestep, include_forecast):
    path, bounds, zone = export_history
    expected = legacy_export(path, bounds, timestep, zone, include_forecast)
    actual = data.analysis_export_dataframe(path, bounds, timestep, zone, include_forecast=include_forecast)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("include_forecast", [False, True])
def test_export_uses_one_query_and_skips_unrequested_forecast_history(download_database, include_forecast):
    path, bounds, zone = download_database
    engine = data.create_engine_for_database(path)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        frame = data.analysis_export_dataframe(path, bounds, "hour", zone, include_forecast=include_forecast)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(frame) == 1
    assert len(statements) == 1
    assert ("forecast_solar" in statements[0]) == include_forecast


@pytest.mark.parametrize("populated", ["neither", "actual", "forecast"])
def test_export_preserves_empty_and_one_sided_ranges(tmp_path, populated):
    path = str(tmp_path / "empty.db")
    engine = create_engine(f"sqlite:///{path}")
    SigenStorModbusSample.metadata.create_all(
        engine, tables=[SigenStorModbusSample.__table__, ForecastSolarSample.__table__]
    )
    zone = ZoneInfo("Europe/Dublin")
    stamp = datetime(2026, 10, 14, tzinfo=zone).isoformat()
    with Session(engine) as session:
        if populated == "actual":
            session.add(SigenStorModbusSample(collected_at_utc=stamp))
        elif populated == "forecast":
            session.add(ForecastSolarSample(collected_at_utc=stamp, forecast_time=stamp))
        session.commit()
    engine.dispose()
    bounds = data.analysis_date_bounds(date(2026, 10, 14), date(2026, 10, 14), zone)
    expected = legacy_export(path, bounds, "hour", zone, True)
    actual = data.analysis_export_dataframe(path, bounds, "hour", zone, include_forecast=True)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False)


def test_excel_preserves_rows_blanks_and_local_datetimes():
    frame = pd.DataFrame({
        "period": [pd.Timestamp("2026-10-14T10:00:00", tz="Europe/Dublin"), pd.NaT],
        "energy": [1.25, float("nan")],
        "band": ["day", "night"],
        "limit": [float("inf"), float("-inf")],
    })
    with ZipFile(BytesIO(excel_bytes(frame))) as workbook:
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        namespaces = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        cells = {cell.attrib["r"]: cell for cell in sheet.findall("m:sheetData/m:row/m:c", namespaces)}
        assert cells["C2"].find("m:is/m:t", namespaces).text == "day"
        assert cells["C3"].find("m:is/m:t", namespaces).text == "night"
        assert cells["D2"].find("m:is/m:t", namespaces).text == "inf"
        assert cells["D3"].find("m:is/m:t", namespaces).text == "-inf"
        assert float(cells["B2"].find("m:v", namespaces).text) == 1.25
        # A formatted empty date cell is still blank in Excel.
        for address in ("A3", "B3"):
            if address in cells:
                assert cells[address].find("m:v", namespaces) is None
                assert cells[address].find("m:is", namespaces) is None
        local = datetime(2026, 10, 14, 10)
        serial = (local - datetime(1899, 12, 30)).total_seconds() / 86400
        assert float(cells["A2"].find("m:v", namespaces).text) == pytest.approx(serial)
