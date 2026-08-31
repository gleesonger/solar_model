from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from nicegui.elements.column import Column

from common import LOGGER
from config import Config

from .historical import HistoricalTab
from .hourly import HourlyTab
from .live import LivePowerCollector
from .models import DashboardElements, DashboardState
from .recent import RecentTab
from .system_info import SystemInfoTab


class DashboardLayout:
    def __init__(
        self,
        config: Config,
        live_collector: LivePowerCollector,
    ) -> None:
        self.config = config
        self.elements = DashboardElements()
        self.timezone = ZoneInfo(config.timezone)
        today = datetime.now(self.timezone).date()
        self.state = DashboardState(hourly_start=today, hourly_end=today)

        self.recent_tab = RecentTab(config, self.state, self.elements, live_collector)
        self.hourly_tab = HourlyTab(config, self.state, self.elements)
        self.historical_tab = HistoricalTab(config, self.state, self.elements)
        self.system_info_tab = SystemInfoTab(config, self.elements)

    def render_dashboard_layout(self, container: Column) -> None:
        LOGGER.info("Dashboard page UI loading")
        container.clear()
        with container:
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Solar dashboard").classes("text-2xl font-bold")
                ui.button("Refresh", on_click=self.refresh_dashboard, icon="refresh")
            self.elements.updated_at_label = ui.label(
                f"Last updated: {datetime.now(self.timezone):%Y-%m-%d %H:%M:%S %Z}"
            ).classes("text-sm text-gray-600")

            with ui.tabs(
                on_change=lambda event: setattr(self.state, "active_tab", str(event.value)),
            ).classes("solar-tabs w-full") as tabs:
                recent = ui.tab("Recent")
                hourly = ui.tab("Data by Hour")
                historical = ui.tab("Historical")
                system_info = ui.tab("System Info")
            selected = {
                "Recent": recent,
                "Data by Hour": hourly,
                "Historical": historical,
                "System Info": system_info,
            }.get(self.state.active_tab, recent)

            with ui.tab_panels(tabs, value=selected).classes("w-full"):
                with ui.tab_panel(recent).classes("px-0"):
                    self.recent_tab.render_recent_tab()
                with ui.tab_panel(hourly).classes("px-0"):
                    self.hourly_tab.render_hourly_tab()
                with ui.tab_panel(historical).classes("px-0"):
                    self.historical_tab.render_historical_tab()
                with ui.tab_panel(system_info).classes("px-0"):
                    self.system_info_tab.render_system_info_tab()

    def refresh_dashboard(self) -> None:
        LOGGER.info("Dashboard database refresh started")
        try:
            self.recent_tab.refresh_recent_tab()
            self.hourly_tab.refresh_hourly_tab()
            self.historical_tab.refresh_historical_tab()
            self.system_info_tab.refresh_system_info_tab()
        except Exception as error:
            LOGGER.exception("Dashboard database refresh failed")
            ui.notify(f"Unable to refresh dashboard data: {error}", type="negative")
            return
        if self.elements.updated_at_label is not None:
            self.elements.updated_at_label.set_text(
                f"Last updated: {datetime.now(self.timezone):%Y-%m-%d %H:%M:%S %Z}"
            )
        LOGGER.info("Dashboard database refresh completed")

    def refresh_live_power(self) -> None:
        self.recent_tab.refresh_live_power()


def render_dashboard_styles() -> None:
    ui.add_css("""
        .q-table thead th {
            background-color: #0070C0 !important;
            color: #ffffff !important;
        }
    """)
