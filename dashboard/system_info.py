from __future__ import annotations

from nicegui import ui
from nicegui.elements.table import Table

from common import LOGGER
from config import Config

from . import data
from .models import DashboardElements


class SystemInfoTab:
    def __init__(self, config: Config, elements: DashboardElements) -> None:
        self.config = config
        self.elements = elements

    def render_system_info_tab(self, device_information: list[dict[str, str]]) -> None:
        try:
            self.elements.system_table = render_system_information(
                device_information
            )
        except Exception as error:
            LOGGER.exception("Dashboard system information initial load failed")
            ui.label(f"Unable to load system information: {error}").classes("text-red-600")

    def refresh_system_info_tab(self) -> None:
        if self.elements.system_table is not None:
            self.elements.system_table.rows = data.load_device_information(self.config.database.path)

    def apply_loaded_data(self, device_information: list[dict[str, str]]) -> None:
        if self.elements.system_table is not None:
            self.elements.system_table.rows = device_information


def render_system_information(device_information: list[dict[str, str]]) -> Table:
    ui.label("System information").classes("text-lg font-semibold")
    columns = [
        {"name": "variable", "label": "Variable", "field": "variable", "align": "left"},
        {"name": "value", "label": "Value", "field": "value", "align": "right"},
        {"name": "unit", "label": "Unit", "field": "unit", "align": "left"},
    ]
    return ui.table(columns=columns, rows=device_information, row_key="variable").props("dense flat bordered").classes("w-full max-w-3xl")
