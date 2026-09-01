from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from nicegui.elements.column import Column

from common import LOGGER
from config import Config

from .anlaysis import AnalysisTab
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
        self.state = DashboardState(
            hourly_start=today,
            hourly_end=today,
            historical_as_of=today,
        )

        self.recent_tab = RecentTab(config, self.state, self.elements, live_collector)
        self.historical_tab = AnalysisTab(config, self.state, self.elements)
        self.system_info_tab = SystemInfoTab(config, self.elements)

    def render_dashboard_layout(self, container: Column) -> None:
        LOGGER.info("Dashboard page UI loading")
        container.clear()
        analysis_controls = None

        def tab_changed(event) -> None:
            self.state.active_tab = str(event.value)
            if analysis_controls is not None:
                analysis_controls.set_visibility(self.state.active_tab == "Analysis")

        with container:
            with ui.element("div").classes("dashboard-topbar w-full"):
                with ui.element("div").classes("grid w-full grid-cols-[1fr_auto_1fr] items-center py-1"):
                    ui.element("div")
                    with ui.tabs(
                        on_change=tab_changed,
                    ).classes("solar-tabs") as tabs:
                        recent = ui.tab("Recent")
                        historical = ui.tab("Analysis")
                        system_info = ui.tab("System Info")
                    ui.button("Refresh", on_click=self.refresh_dashboard, icon="refresh").classes(
                        "justify-self-end"
                    )
                analysis_controls = self.historical_tab.render_historical_controls()
                analysis_controls.set_visibility(self.state.active_tab in {"Analysis", "Historical"})
            # self.elements.updated_at_label = ui.label(
            #     f"Last Full Update: {datetime.now(self.timezone):%Y-%m-%d %H:%M:%S %Z}"
            # ).classes("text-sm text-gray-600")
            selected = {
                "Recent": recent,
                "Analysis": historical,
                "Historical": historical,
                "System Info": system_info,
            }.get(self.state.active_tab, recent)

            with ui.tab_panels(tabs, value=selected).classes("w-full"):
                with ui.tab_panel(recent).classes("p-0"):
                    self.recent_tab.render_recent_tab()
                with ui.tab_panel(historical).classes("p-0"):
                    self.historical_tab.render_historical_tab()
                with ui.tab_panel(system_info).classes("p-0"):
                    self.system_info_tab.render_system_info_tab()

    def refresh_dashboard(self) -> None:
        LOGGER.info("Dashboard database refresh started")
        try:
            self.recent_tab.refresh_recent_tab()
            self.historical_tab.refresh_historical_tab()
            self.system_info_tab.refresh_system_info_tab()
        except Exception as error:
            LOGGER.exception("Dashboard database refresh failed")
            ui.notify(f"Unable to refresh dashboard data: {error}", type="negative")
            return
        # if self.elements.updated_at_label is not None:
        #     self.elements.updated_at_label.set_text(
        #         f"Last Full Update: {datetime.now(self.timezone):%Y-%m-%d %H:%M:%S %Z}"
        #     )
        LOGGER.info("Dashboard database refresh completed")

    def refresh_live_power(self) -> None:
        self.recent_tab.refresh_live_power()


def render_dashboard_styles() -> None:
    ui.add_css("""
        .dashboard-topbar {
            position: sticky;
            top: 0;
            z-index: 30;
            background: white;
        }
        .analysis-controls {
            background: white;
            padding-top: 0.5rem;
            padding-bottom: 0.5rem;
        }
        .q-table thead th {
            background-color: #0070C0 !important;
            color: #ffffff !important;
        }
    """)
