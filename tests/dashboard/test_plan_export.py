"""Downloads include measured telemetry without modifying the saved projection."""
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pandas as pd
import pytest

from solar_model.dashboard.components.plan_detail import detail_excel
from solar_model.planning.plan_report import detail_frame
from tests.dashboard.test_plan import sample_result


def sheet_rows(content: bytes, number: int) -> list[dict[str, str]]:
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(content)) as workbook:
        strings = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            strings = ["".join(text.text or "" for text in item.findall(".//s:t", namespace)) for item in shared]
        sheet = ElementTree.fromstring(workbook.read(f"xl/worksheets/sheet{number}.xml"))
        rows = []
        for row in sheet.findall("s:sheetData/s:row", namespace):
            values = {}
            for cell in row:
                value = cell.find("s:v", namespace)
                if value is not None:
                    values[cell.attrib["r"]] = strings[int(value.text)] if cell.attrib.get("t") == "s" else value.text
                elif cell.attrib.get("t") == "inlineStr":
                    values[cell.attrib["r"]] = "".join(text.text or "" for text in cell.findall(".//s:t", namespace))
            rows.append(values)
        return rows


def records(content: bytes) -> list[dict[str, str]]:
    rows = sheet_rows(content, 1)
    header_index = next(index for index, row in enumerate(rows) if "Start" in row.values() or "Start (local)" in row.values())
    headers = {cell.rstrip("0123456789"): label for cell, label in rows[header_index].items()}
    return [{headers[cell.rstrip("0123456789")]: value for cell, value in row.items()} for row in rows[header_index + 1:]]


def test_actuals_share_plan_rows_with_units_precision_coverage_and_view_bounds() -> None:
    result = sample_result()
    frame = detail_frame(result.results["med"], 20.)
    original = frame.copy(deep=True)
    actuals = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-10-27T09:00Z", "2026-10-27T10:05Z", "2026-10-27T10:19Z", "2026-10-27T10:20Z", "2026-10-27T10:30Z", "2026-10-27T11:21Z"]),
        "solar": [99., 1.234567891, 3., float("nan"), float("nan"), 999.],
        "house_load": [99., 1., 1., 2., 4., 999.], "battery": [99., 5., 4.5, 4., 3., 999.],
    })
    content = detail_excel(frame, "Europe/Dublin", result.next_action.mode, "Compact", actuals=actuals)
    with ZipFile(BytesIO(content)) as workbook:
        metadata = workbook.read("xl/workbook.xml").decode()
        assert 'name="Plan detail"' in metadata and 'name="Actuals"' not in metadata
        assert 'xl/worksheets/sheet2.xml' not in workbook.namelist()
    rows = records(content)
    assert len(rows) == 3
    assert float(rows[0]["Forecast Solar (kW)"]) == 2.
    assert float(rows[0]["Actual Solar (kW)"]) == pytest.approx((1.234567891 + 3.) / 2, abs=1e-12)
    assert float(rows[0]["Actual Solar coverage (min)"]) == 2.
    assert "Actual Solar (kW)" not in rows[1]
    assert float(rows[1]["Actual House Load (kW)"]) == 3.
    assert float(rows[0]["Actual Battery (kWh)"]) == 4.
    assert rows[0]["Actual Battery timestamp (UTC)"] == "2026-10-27T10:20:00+00:00"
    assert float(rows[1]["Actual Battery (kWh)"]) == 3.
    assert "Actual Battery (kWh)" not in rows[-1]
    assert "Forecast Solar (kW)" not in rows[-1]
    pd.testing.assert_frame_equal(frame, original)


def test_empty_actuals_leave_comparison_cells_blank() -> None:
    result = sample_result()
    frame = detail_frame(result.results["med"], 20.)
    content = detail_excel(frame, "UTC", result.next_action.mode, "Advanced", actuals=pd.DataFrame())
    rows = records(content)
    assert rows[0]["Start (UTC)"] == "2026-10-27T10:05:00+00:00"
    assert rows[0]["Solar forecast (kW)"] == "2"
    assert all(not any(" actual" in name for name in row) for row in rows)


def test_repeated_local_hour_is_aligned_using_distinct_utc_intervals() -> None:
    result = sample_result()
    frame = detail_frame(result.results["med"], 20.)
    frame["starts_at"] = pd.to_datetime(["2026-10-25T00:00Z", "2026-10-25T01:00Z"])
    frame["ends_at"] = frame.starts_at + pd.Timedelta(hours=1)
    actuals = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-25T00:30Z", "2026-10-25T01:30Z"]), "solar": [2., 5.], "battery": [5., 4.]})
    content = detail_excel(frame, "Europe/Dublin", result.next_action.mode, "Compact", actuals=actuals)
    rows = records(content)
    assert rows[0]["Start"] == rows[1]["Start"] == "2026-10-25 01:00:00"
    assert rows[0]["Start (UTC)"] != rows[1]["Start (UTC)"]
    assert rows[0]["Actual Solar (kW)"] == "2" and rows[1]["Actual Solar (kW)"] == "5"


def test_partial_minutes_are_weighted_by_overlap() -> None:
    from solar_model.dashboard.components.plan_detail import interval_actuals
    frame = detail_frame(sample_result().results["med"], 20.).iloc[:1].copy()
    frame["starts_at"] = pd.to_datetime(["2026-10-27T10:05:30Z"])
    frame["ends_at"] = pd.to_datetime(["2026-10-27T10:06:30Z"])
    actuals = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-27T10:05Z", "2026-10-27T10:06Z"]), "solar": [2., 4.]})
    aligned = interval_actuals(frame, actuals)
    assert aligned["actual_solar_kw"].iloc[0] == 3.
    assert aligned["actual_solar_coverage_mins"].iloc[0] == 1.


def test_advanced_export_order_metadata_units_and_single_battery_forecast() -> None:
    from solar_model.dashboard.components.plan_detail import ADVANCED_EXPORT_COLUMNS
    result = sample_result()
    frame = detail_frame(result.results["med"], 20.)
    actuals = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-27T10:05Z"]), "solar": [1.234567891], "battery": [4.]})
    content = detail_excel(frame, "Europe/Dublin", result.next_action.mode, "Advanced", actuals=actuals, report_basis="Stress case", metadata={"snapshot_id": "saved-123", "model": "Markov DP", "solar_scenario": "med", "house_load_scenario": "high"})
    rows = sheet_rows(content, 1)
    assert [list(row.values()) for row in rows[:6]] == [["snapshot_id", "saved-123"], ["model", "Markov DP"], ["view", "Advanced"], ["solar_scenario", "med"], ["house_load_scenario", "high"], ["report_basis", "Stress case"]]
    headers = next(list(row.values()) for row in rows if "Start (local)" in row.values())
    assert headers == [label for _, _, label in ADVANCED_EXPORT_COLUMNS]
    assert len(headers) == len(set(headers)) == 52
    assert sum("Battery" in label and "forecast" in label and "(kWh)" in label for label in headers) == 1
    data = records(content)
    assert float(data[0]["Duration (min)"]) == 15.
    assert float(data[1]["Duration (min)"]) == 60.
    assert float(data[-1]["Duration (min)"]) == 75.
    assert float(data[0]["Solar actual (kW)"]) == pytest.approx(1.234567891, abs=1e-12)
    assert float(data[0]["Battery actual latest (kWh)"]) == 4.
    assert data[0]["Battery actual reading time (UTC)"] == "2026-10-27T10:05:00+00:00"
    assert "Solar actual (kW)" not in data[-1]
    assert "Battery forecast end (kWh)" not in data[-1]
    assert "report_basis" not in headers
