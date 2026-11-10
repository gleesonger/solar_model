"""Shared saved/live plan table rendering and spreadsheet export."""
from __future__ import annotations

from html import escape
from typing import Any

import pandas as pd
from nicegui import ui

from solar_model.dashboard.components.download_dialog import excel_bytes
from solar_model.dashboard.components.tables import mark_final_total_row
from solar_model.planning.plan_report import advanced_detail_totals, detail_column, detail_columns, detail_decimal_places, detail_rows, detail_totals, table_detail_frame
from solar_model.planning.types import OperatingMode


def render_detail_table(frame: pd.DataFrame, timezone_name: str, mode: OperatingMode, view: str) -> list[dict[str, Any]]:
    columns = detail_columns(frame, compact=view == "Compact")
    display = table_detail_frame(frame)
    table = ui.table(columns=columns, rows=detail_rows(display, timezone_name, mode), row_key="period", pagination=None).props("dense flat bordered").classes("w-full max-w-full")
    if view == "Advanced":
        table.classes("plan-detail-table--advanced")
    total_frame = frame.assign(t=display.t)
    footer_rows = advanced_detail_totals(total_frame) if view == "Advanced" else [detail_totals(total_frame)]
    footer_html = []
    for index, totals in enumerate(footer_rows):
        for column in columns:
            name = column["name"]
            if totals.get(name) is not None and detail_decimal_places(name) is not None:
                totals[name] = f"{totals[name]:.{detail_decimal_places(name)}f}"
        row_class = ' class="plan-subtotal-row"' if index < len(footer_rows) - 1 else ""
        footer_html.append("<q-tr" + row_class + ">" + "".join(
            f'<q-td class="text-{column["align"]}">{escape(str(totals[column["name"]])) if totals.get(column["name"]) is not None else ""}</q-td>'
            for column in columns
        ) + "</q-tr>")
    table.add_slot("bottom-row", "".join(footer_html))
    mark_final_total_row(table).classes("plan-detail-table")
    ui.element("div").classes("h-16")
    return columns


def detail_excel(frame: pd.DataFrame, timezone_name: str, mode: OperatingMode, view: str, *, report_basis: str | None = None) -> bytes:
    columns = [column for column in detail_columns(frame, compact=view == "Compact") if detail_column(column["name"]).export]
    rows = detail_rows(frame, timezone_name, mode, time_format="%Y-%m-%d %H:%M:%S")
    hours = (frame.ends_at - frame.starts_at).dt.total_seconds() / 3600
    total_frame = frame.assign(t=hours)
    footers = advanced_detail_totals(total_frame) if view == "Advanced" else [detail_totals(total_frame)]
    exported = pd.DataFrame([*rows, *footers]).reindex(columns=[column["name"] for column in columns])
    exported = exported.rename(columns={column["name"]: column["label"].replace("\n", " ") for column in columns})
    if report_basis is not None:
        exported["Report basis"] = report_basis
    return excel_bytes(exported, sheet_name="Plan detail")
