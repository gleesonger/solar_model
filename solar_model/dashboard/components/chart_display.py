"""Reusable chart presentation with data export actions."""

from __future__ import annotations

from copy import deepcopy
import re
from collections.abc import Callable, Mapping, Sequence
from numbers import Real
from typing import Any

import pandas as pd

from nicegui import ui

from solar_model.dashboard.components.tables import mark_final_total_row
from solar_model.dashboard.components.warnings import notify_success
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
        column_labels: Mapping[str, str] | None = None,
        decimal_places: Mapping[str, int] | None = None,
        table_columns: Sequence[str] | None = None,
        show_total: bool = False,
        total_aggregations: Mapping[str, str] | None = None,
        header_controls: Callable[[], None] | None = None,
        show_series_selection: bool = False,
    ) -> None:
        self.dataframe = dataframe
        self._title = title
        self._hint = hint
        self._chart_options = chart_options
        self._column_labels = column_labels or {}
        self._decimal_places = decimal_places or {}
        self._table_column_order = table_columns
        self._show_total = show_total
        self._total_aggregations = total_aggregations or {}
        self._showing_table = False
        self._series_selection_state = "Default"

        with ui.row().classes("w-full items-center justify-between gap-4 mt-4"):
            self.title = ui.label(self._text(title)).classes("text-lg font-semibold")

            with ui.row().classes("items-center gap-1 rounded border p-1"):
                if header_controls is not None:
                    header_controls()
                if show_series_selection:
                    self._series_selection_button = ui.button("Default", on_click=self._cycle_series_selection).props("flat dense")
                    self._series_selection_button.tooltip("Cycle series: None → All → Default")
                ui.button(icon="content_copy", on_click=self._copy_data).props("flat round dense").tooltip("Copy data for Excel")
                ui.button(icon="download", on_click=self._download_data).props("flat round dense").tooltip("Download CSV")
                ui.button(icon="table_chart", on_click=self._toggle_layout).props("flat round dense").tooltip("Show table")
        self.hint = ui.label(self._text(hint)).classes("text-sm text-gray-600") if hint is not None else None

        self.chart_container = ui.column().classes("w-full")
        with self.chart_container:
            self.chart = render_chart()
        self._known_series_names = {series.get("name") for series in self.chart.options.get("series", [])}
        self._default_series_selection = dict(self.chart.options.get("legend", {}).get("selected", {}))
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
            for name, selected in options.get("legend", {}).get("selected", {}).items():
                self._default_series_selection.setdefault(name, selected)
            self.chart.options.clear()
            self.chart.options.update(options)
            browser_options = deepcopy(options)
            legend = browser_options.get("legend")
            if isinstance(legend, dict):
                defaults = legend.pop("selected", {})
                new_defaults = {name: selected for name, selected in defaults.items() if name not in self._known_series_names}
                if new_defaults:
                    legend["selected"] = new_defaults
            self._known_series_names.update(series.get("name") for series in options.get("series", []))
            self.chart.run_chart_method("setOption", browser_options, {"replaceMerge": ["series"]})

    def _cycle_series_selection(self) -> None:
        self._series_selection_state = {"Default": "None", "None": "All", "All": "Default"}[self._series_selection_state]
        selection = {
            series["name"]: (
                self._default_series_selection.get(series["name"], True)
                if self._series_selection_state == "Default" else self._series_selection_state == "All"
            )
            for series in self.chart.options.get("series", [])
        }
        self.chart.options.setdefault("legend", {})["selected"] = selection
        self.chart.run_chart_method("setOption", {"legend": {"selected": selection}})
        self._series_selection_button.set_text(self._series_selection_state)

    def refresh_chart(self) -> None:
        """Re-render the chart from the current dataframe and chart options."""
        if self._chart_options is None:
            return
        options = self._chart_options(self.dataframe)
        self.chart.options.clear()
        self.chart.options.update(options)
        browser_options = deepcopy(options)
        self.chart.run_chart_method("setOption", browser_options, {"notMerge": True})

    @staticmethod
    def _text(value: str | Callable[[], str]) -> str:
        return value() if callable(value) else value

    def _render_table(self):
        with self.table_container:
            return mark_final_total_row(ui.table(
                columns=self._table_columns(),
                rows=self._table_rows(),
                row_key="_row",
            ).props("dense flat bordered").classes("max-w-full").style("width: fit-content"))

    def _table_columns(self) -> list[dict[str, str]]:
        return [
            {
                "name": column,
                "label": self._column_labels.get(column, column.replace("_", " ").title()),
                "field": column,
                "align": "right",
            }
            for column in self._table_column_names()
        ]

    def _table_column_names(self) -> list[str]:
        if self._table_column_order is None:
            return list(self.dataframe.columns)
        return [column for column in self._table_column_order if column in self.dataframe]

    def _table_rows(self) -> list[dict[str, object]]:
        rows = self.dataframe.astype(object).where(pd.notna(self.dataframe), "").to_dict(orient="records")
        formatted_rows = [
            {
                "_row": index,
                **{column: self._format_table_value(value, column) for column, value in row.items()},
            }
            for index, row in enumerate(rows)
        ]
        if self._show_total and not self.dataframe.empty:
            total_row: dict[str, object] = {"_row": "total"}
            for index, column in enumerate(self._table_column_names()):
                if index == 0:
                    total_row[column] = "Total"
                elif pd.api.types.is_numeric_dtype(self.dataframe[column]):
                    series = self.dataframe[column]
                    aggregation = self._total_aggregations.get(column, "sum")
                    if aggregation == "mean":
                        value = series.mean()
                    else:
                        value = series.sum(min_count=1)
                    total_row[column] = self._format_table_value(value, column)
                else:
                    total_row[column] = ""
            formatted_rows.append(total_row)
        return formatted_rows

    def _format_table_value(self, value: object, column: str) -> object:
        if value is None or (isinstance(value, Real) and pd.isna(value)):
            return ""
        if isinstance(value, Real) and not isinstance(value, bool):
            return f"{float(value):.{self._decimal_places.get(column, 2)}f}"
        return value

    def _copy_data(self) -> None:
        text = self.dataframe.to_csv(index=False, sep="\t", lineterminator="\n")
        ui.clipboard.write(text)
        notify_success("Chart data copied to clipboard")

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
