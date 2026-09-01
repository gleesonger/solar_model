"""Reusable chart presentation with data export actions."""

from __future__ import annotations

from copy import deepcopy
import re
from collections.abc import Callable
from numbers import Real
from typing import Any

import pandas as pd
from nicegui import ui
from nicegui.elements.echart import EChart


class ChartDataDisplay:
    """Display a chart and its source dataframe with consistent export controls.

    The render callback creates the chart inside the component, keeping ownership
    of the layout and chart lifecycle in one place. It can also bind ECharts
    events such as data zoom while creating the chart.
    """

    def __init__(
        self,
        *,
        dataframe: pd.DataFrame,
        title: str | Callable[[], str],
        render_chart: Callable[[], EChart],
        hint: str | Callable[[], str] | None = None,
        chart_options: Callable[[pd.DataFrame], dict[str, Any]] | None = None,
    ) -> None:
        self.dataframe = dataframe
        self._title = title
        self._hint = hint
        self._chart_options = chart_options
        self._showing_table = False

        with ui.row().classes("w-full items-center justify-between gap-4 mt-4"):
            self.title = ui.label(self._text(title)).classes("text-lg font-semibold")

            with ui.row().classes("items-center gap-1 rounded border p-1"):
                ui.button(icon="content_copy", on_click=self._copy_data).props("flat round dense").tooltip("Copy data for Excel")
                ui.button(icon="download", on_click=self._download_data).props("flat round dense").tooltip("Download CSV")
                ui.button(icon="table_chart", on_click=self._toggle_layout).props("flat round dense").tooltip("Show table")
        self.hint = (
            ui.label(self._text(hint)).classes("text-sm text-gray-600")
            if hint is not None
            else None
        )

        self.chart_container = ui.column().classes("w-full")
        with self.chart_container:
            self.chart = render_chart()
        self.table_container = ui.column().classes("w-full overflow-auto")
        self.table = self._render_table()
        self.table_container.set_visibility(False)

    def update(self, dataframe: pd.DataFrame) -> None:
        self.dataframe = dataframe
        self.title.set_text(self._text(self._title))
        if self.hint is not None and self._hint is not None:
            self.hint.set_text(self._text(self._hint))
        self.table.columns = self._table_columns()
        self.table.rows = self._table_rows()
        self.table.update()
        if self._chart_options is not None:
            options = self._chart_options(dataframe)
            self.chart.options.clear()
            self.chart.options.update(options)
            browser_options = deepcopy(options)
            legend = browser_options.get("legend")
            if isinstance(legend, dict):
                legend.pop("selected", None)
            self.chart.run_chart_method("setOption", browser_options)

    @staticmethod
    def _text(value: str | Callable[[], str]) -> str:
        return value() if callable(value) else value

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
        rows = (
            self.dataframe.astype(object)
            .where(pd.notna(self.dataframe), "")
            .to_dict(orient="records")
        )
        return [
            {
                "_row": index,
                **{column: self._format_table_value(value) for column, value in row.items()},
            }
            for index, row in enumerate(rows)
        ]

    @staticmethod
    def _format_table_value(value: object) -> object:
        if isinstance(value, Real) and not isinstance(value, bool):
            return f"{float(value):.2f}"
        return value

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
