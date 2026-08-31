from __future__ import annotations

import os
from pathlib import Path

from nicegui import app, ui

from config import load_config
from dashboard.layout import DashboardLayout, render_dashboard_styles
from dashboard.live import LivePowerCollector
from common import source_path

CONFIG_PATH = source_path("config.yaml")

def main() -> None:
    config = load_config(CONFIG_PATH)

    live_collector = LivePowerCollector(
        config.actuals.modbus,
        config.timezone,
        config.live.scheduler.interval_seconds,
        config.dashboard.actuals_to_forecast,
    )
    app.on_startup(live_collector.start)
    app.on_shutdown(live_collector.stop)

    @ui.page("/")
    def render_dashboard_page() -> None:
        render_dashboard_styles()
        layout = DashboardLayout(
            config,
            live_collector,
        )
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            layout.render_dashboard_layout(container)
        ui.timer(config.live.scheduler.interval_seconds, layout.refresh_live_power)
        ui.timer(60, layout.refresh_dashboard)

    ui.run(
        host=config.dashboard.host,
        port=config.dashboard.port,
        title="Solar dashboard",
        reload=False,
    )


if __name__ == "__main__":
    main()
