from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui
from nicegui.elements.column import Column

from common import LOGGER
from config import Config

from .anlaysis import AnalysisTab
from .control import ControlTab
from . import data
from .live import LivePowerCollector
from .models import DashboardElements, DashboardState
from .recent import RecentTab
from .system_info import SystemInfoTab
from .snapshot import DashboardSnapshot


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
        self.control_tab = ControlTab(config)

    def render_dashboard_layout(self, container: Column, snapshot: DashboardSnapshot) -> None:
        LOGGER.info("Dashboard page UI loading")
        container.clear()

        def tab_changed(event) -> None:
            self.state.active_tab = str(event.value)
            if self.state.active_tab == "Analysis" and self.elements.historical_charts is None:
                with historical_panel:
                    self.historical_tab.render_historical_tab()
            self.historical_tab.set_controls_visibility(self.state.active_tab == "Analysis")

        with container:
            with ui.element("div").classes("dashboard-topbar w-full"):
                with ui.element("div").classes("dashboard-tabs-row grid w-full grid-cols-[1fr_auto_1fr] items-center py-1"):
                    ui.element("div")
                    with ui.tabs(
                        on_change=tab_changed,
                    ).classes("solar-tabs") as tabs:
                        recent = ui.tab("Recent")
                        historical = ui.tab("Analysis")
                        system_info = ui.tab("System Info")
                        control = ui.tab("Control")
                    self.historical_tab.render_mobile_controls_trigger()
                # This status is deliberately outside normal document flow.
                # Its initial visibility update arrives over the WebSocket and
                # must not make the tab content jump as it is hidden/shown.
                self.elements.updated_at_label = ui.label().classes(
                    "dashboard-updated-label text-sm font-semibold text-white "
                    "bg-red-600 border-4 border-red-900 rounded px-2 py-1"
                )
                self.elements.updated_at_label.set_visibility(False)
                self.historical_tab.render_historical_controls()
                self.historical_tab.set_controls_visibility(self.state.active_tab in {"Analysis", "Historical"})
            selected = {
                "Recent": recent,
                "Analysis": historical,
                "Historical": historical,
                "System Info": system_info,
                "Control": control,
            }.get(self.state.active_tab, recent)

            with ui.tab_panels(tabs, value=selected).classes("w-full"):
                with ui.tab_panel(recent).classes("p-0"):
                    self.recent_tab.render_recent_tab(snapshot.recent_data)
                with ui.tab_panel(historical).classes("p-0") as historical_panel:
                    if self.state.active_tab == "Analysis":
                        self.historical_tab.render_historical_tab()
                with ui.tab_panel(system_info).classes("p-0"):
                    self.system_info_tab.render_system_info_tab(snapshot.device_information)
                with ui.tab_panel(control).classes("p-0"):
                    self.control_tab.render_control_tab()

    def apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        """Apply shared data to this client's existing UI elements."""
        self.recent_tab.apply_loaded_data(snapshot.recent_data)
        self.system_info_tab.apply_loaded_data(snapshot.device_information)

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
        .dashboard-updated-label {
            position: absolute;
            top: 0.25rem;
            right: 1rem;
            z-index: 1;
        }
        .analysis-controls {
            background: white;
            padding-top: 0.5rem;
            padding-bottom: 0.5rem;
        }
        .analysis-mobile-controls-trigger {
            display: none;
        }
        .analysis-mobile-controls-trigger-hidden {
            visibility: hidden;
            pointer-events: none;
        }
        .analysis-mobile-drawer {
            width: min(22rem, 90vw);
            padding: 1rem;
        }
        @media (max-width: 767px) {
            .analysis-controls-desktop {
                display: none !important;
            }
            .analysis-mobile-controls-trigger {
                display: inline-flex;
                justify-self: start;
                grid-column: 1;
                grid-row: 1;
                margin-right: 0.25rem;
            }
            .dashboard-tabs-row {
                grid-template-columns: auto auto 1fr !important;
            }
            .dashboard-tabs-row > :first-child {
                display: none;
            }
            .solar-tabs {
                justify-self: start;
                grid-column: 2;
                grid-row: 1;
            }
            .live-today-table th,
            .live-today-table td {
                font-size: 0.75rem;
                padding: 3px 4px;
                white-space: normal;
                overflow-wrap: anywhere;
            }
            .live-today-table,
            .live-today-table .q-table__container,
            .live-today-table .q-table,
            .live-today-table table {
                width: 100% !important;
            }
            .live-today-table table {
                table-layout: fixed;
            }
            .live-today-table th:first-child,
            .live-today-table td:first-child {
                width: 34%;
            }
            .live-today-table th:not(:first-child),
            .live-today-table td:not(:first-child) {
                width: 22%;
            }
        }
        .q-table thead th {
            background-color: #0070C0 !important;
            color: #ffffff !important;
        }
        .summary-total-table tbody tr:last-child td {
            background-color: #f1f3f5 !important;
            font-weight: 700;
        }
    """)
