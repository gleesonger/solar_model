"""Interval-based Analysis data export dialog."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import run, ui

from common import LOGGER

from . import data
from .data import AnalysisRangeBounds


def show_download_data_dialog(
    database_path: str,
    bounds: AnalysisRangeBounds,
    timezone: ZoneInfo,
) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Download Analysis Data").classes("text-lg font-semibold")
        with ui.column().classes("w-full") as form:
            start_input = ui.input("Start date", value=one_year_before(bounds.end_date).isoformat()).props(f"type=date outlined dense max={datetime.now(timezone).date().isoformat()}")
            end_input = ui.input("End date", value=bounds.end_date.isoformat()).props(f"type=date outlined dense max={datetime.now(timezone).date().isoformat()}")
            timestep_input = ui.select(
                ["minute", "hour", "day", "week", "month", "season", "year"], value="hour", label="Timestep"
            ).props("outlined dense").classes("w-48")
            include_forecast = ui.checkbox("Include forecast", value=True)
            format_input = ui.select(["Excel", "CSV"], value="Excel", label="Format").props("outlined dense").classes("w-48")
            ui.label("Energy and costs are summed; power and rates are averaged for each interval.").classes("text-sm text-gray-600")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def download() -> None:
                    try:
                        start_date = date.fromisoformat(str(start_input.value))
                        end_date = date.fromisoformat(str(end_input.value))
                        if end_date > datetime.now(timezone).date():
                            raise ValueError("End date cannot be in the future")
                        selected_bounds = data.analysis_date_bounds(start_date, end_date, timezone)
                        form.set_visibility(False)
                        progress.set_visibility(True)
                        content, filename, media_type = await run.io_bound(
                            prepare_download,
                            database_path,
                            selected_bounds,
                            str(timestep_input.value),
                            timezone,
                            bool(include_forecast.value),
                            str(format_input.value),
                        )
                    except Exception as error:
                        progress.set_visibility(False)
                        form.set_visibility(True)
                        ui.notify(f"Unable to prepare data download: {error}", type="negative")
                        return

                    ui.download(content, filename=filename, media_type=media_type)
                    dialog.close()

                ui.button("Download", on_click=download, icon="download")

        with ui.column().classes("w-full items-center gap-3") as progress:
            ui.spinner(size="lg")
            ui.label("Preparing download...")
        progress.set_visibility(False)
    dialog.open()


def prepare_download(
    database_path: str,
    bounds: AnalysisRangeBounds,
    timestep: str,
    timezone: ZoneInfo,
    include_forecast: bool,
    file_format: str,
) -> tuple[bytes, str, str]:
    exported = data.analysis_export_dataframe(
        database_path, bounds, timestep, timezone, include_forecast=include_forecast
    )
    filename_base = f"analysis-data-{bounds.start_date:%Y-%m-%d}-to-{bounds.end_date:%Y-%m-%d}"
    if file_format == "CSV":
        return exported.to_csv(index=False).encode(), f"{filename_base}.csv", "text/csv"
    return (
        excel_bytes(exported),
        f"{filename_base}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def excel_bytes(dataframe: pd.DataFrame) -> bytes:
    # Excel has no timezone-aware datetime type.  Keep the displayed local
    # interval, but remove its timezone metadata for the workbook.
    exported = dataframe.copy()
    for column in exported.select_dtypes(include=["datetimetz"]):
        exported[column] = exported[column].dt.tz_localize(None)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        exported.to_excel(writer, sheet_name="Analysis Data", index=False)
    return buffer.getvalue()


def one_year_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)
