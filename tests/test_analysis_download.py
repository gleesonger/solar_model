from datetime import date, datetime
from io import BytesIO
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dashboard import data
from dashboard.download_data_dialog import prepare_download
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
            assert "day / night" in workbook.read("xl/sharedStrings.xml").decode()


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
