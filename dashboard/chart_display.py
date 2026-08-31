"""Reusable chart presentation with data export actions."""

from __future__ import annotations

import re
from collections.abc import Callable

import pandas as pd
from nicegui import ui
from nicegui.elements.echart import EChart
from nicegui.elements.label import Label


class ChartDataDisplay:
    """Display a chart and its source dataframe with consistent export controls.

    A caller may supply an existing chart or a callback that creates one inside the
    component. The latter is useful when the callback also needs to bind chart
    events such as ECharts data zoom.
    """

    def __init__(
        self,
        *,
        dataframe: pd.DataFrame,
        title: str,
        hint: str,
        chart: EChart | None = None,
        render_chart: Callable[[], EChart] | None = None,
    ) -> None:
        if chart is None and render_chart is None:
            raise ValueError("Provide either chart or render_chart")
        if chart is not None and render_chart is not None:
            raise ValueError("Provide chart or render_chart, not both")

        self.dataframe = dataframe
        self.title = ui.label(title).classes("text-lg font-semibold")
        self.hint = ui.label(hint).classes("text-sm text-gray-600")
        self._showing_table = False

        with ui.row().classes("w-full items-start justify-between gap-4 mt-4"):
            with ui.column().classes("gap-0"):
                self.title.move()
                self.hint.move()
            with ui.row().classes("items-center gap-1 rounded border p-1"):
                ui.button(icon="content_copy", on_click=self._copy_data).props("flat round dense").tooltip(
                    "Copy data for Excel"
                )
                ui.button(icon="download", on_click=self._download_data).props("flat round dense").tooltip(
                    "Download CSV"
                )
                ui.button(icon="table_chart", on_click=self._toggle_layout).props("flat round dense").tooltip(
                    "Show table"
                )

        self.chart_container = ui.column().classes("w-full")
        with self.chart_container:
            self.chart = render_chart() if render_chart is not None else chart
            if chart is not None:
                chart.move()
        self.table_container = ui.column().classes("w-full overflow-auto")
        self.table = self._render_table()
        self.table_container.set_visibility(False)

    def update(self, dataframe: pd.DataFrame, *, title: str | None = None, hint: str | None = None) -> None:
        self.dataframe = dataframe
        if title is not None:
            self.title.set_text(title)
        if hint is not None:
            self.hint.set_text(hint)
        self.table.columns = self._table_columns()
        self.table.rows = self._table_rows()
        self.table.update()

    def _render_table(self):
        with self.table_container:
            return ui.table(
                columns=self._table_columns(),
                rows=self._table_rows(),
                row_key="_row",
            ).props("dense flat bordered").classes("max-w-full").style("width: fit-content")

    def _table_columns(self) -> list[dict[str, str]]:
        return [
            {"name": column, "label": column.replace("_", " ").title(), "field": column, "align": "right"}
            for column in self.dataframe.columns
        ]

    def _table_rows(self) -> list[dict[str, object]]:
        rows = self.dataframe.where(pd.notna(self.dataframe), None).to_dict(orient="records")
        return [{"_row": index, **row} for index, row in enumerate(rows)]

    def _copy_data(self) -> None:
        text = self.dataframe.to_csv(index=False, sep="\t", lineterminator="\n")
        ui.clipboard.write(text)
        ui.notify("Chart data copied to clipboard")

    def _download_data(self) -> None:
        filename = re.sub(r"[^a-z0-9]+", "-", self.title.text.lower()).strip("-")
        ui.download(
            self.dataframe.to_csv(index=False).encode(),
            filename=f"{filename or 'chart-data'}.csv",
            media_type="text/csv",
        )

    def _toggle_layout(self) -> None:
        self._showing_table = not self._showing_table
        self.chart_container.set_visibility(not self._showing_table)
        self.table_container.set_visibility(self._showing_table)
