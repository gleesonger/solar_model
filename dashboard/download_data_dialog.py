"""Interval-based Analysis data export dialog."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import ui

from . import data
from .data import AnalysisRangeBounds


def show_download_data_dialog(
    database_path: str,
    bounds: AnalysisRangeBounds,
    timezone: ZoneInfo,
) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Download Analysis Data").classes("text-lg font-semibold")
        start_input = ui.input("Start date", value=one_year_before(bounds.end_date).isoformat()).props(
            f"type=date outlined dense max={datetime.now(timezone).date().isoformat()}"
        )
        end_input = ui.input("End date", value=bounds.end_date.isoformat()).props(
            f"type=date outlined dense max={datetime.now(timezone).date().isoformat()}"
        )
        timestep_input = ui.select(
            ["minute", "hour", "day", "week", "month", "season", "year"],
            value="hour",
            label="Timestep",
        ).props("outlined dense").classes("w-48")
        include_forecast = ui.checkbox("Include forecast", value=True)
        format_input = ui.select(["Excel", "CSV"], value="Excel", label="Format").props(
            "outlined dense"
        ).classes("w-48")
        ui.label(
            "Energy and costs are summed; power and rates are averaged for each interval."
        ).classes("text-sm text-gray-600")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def download() -> None:
                try:
                    start_date = date.fromisoformat(str(start_input.value))
                    end_date = date.fromisoformat(str(end_input.value))
                    if end_date > datetime.now(timezone).date():
                        raise ValueError("End date cannot be in the future")
                    selected_bounds = data.analysis_date_bounds(start_date, end_date, timezone)
                    exported = data.analysis_export_dataframe(
                        database_path,
                        selected_bounds,
                        str(timestep_input.value),
                        timezone,
                        include_forecast=bool(include_forecast.value),
                    )
                except Exception as error:
                    ui.notify(f"Unable to prepare data download: {error}", type="negative")
                    return

                filename_base = f"analysis-data-{start_date:%Y-%m-%d}-to-{end_date:%Y-%m-%d}"
                if format_input.value == "CSV":
                    ui.download(exported.to_csv(index=False).encode(), filename=f"{filename_base}.csv", media_type="text/csv")
                else:
                    ui.download(
                        excel_bytes(exported),
                        filename=f"{filename_base}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                dialog.close()

            ui.button("Download", on_click=download, icon="download")
    dialog.open()


def excel_bytes(dataframe: pd.DataFrame) -> bytes:
    # Excel has no timezone-aware datetime type.  Keep the displayed local
    # interval, but remove its timezone metadata for the workbook.
    exported = dataframe.copy()
    for column in exported.select_dtypes(include=["datetimetz"]):
        exported[column] = exported[column].dt.tz_localize(None)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        exported.to_excel(writer, sheet_name="Analysis Data", index=False)
    return buffer.getvalue()


def one_year_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)
