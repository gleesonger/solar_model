from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nicegui import ui

from config import load_config
from dashboard.layout import DashboardLayout, render_dashboard_styles
from dashboard.models import DashboardContext, DashboardState
from common import source_path

CONFIG_PATH = source_path("config.yaml")

def main() -> None:
    config = load_config(CONFIG_PATH)

    context = DashboardContext(
        database_path=config.database.path,
        timezone_name=config.timezone,
        forecast_arrays=config.forecast.arrays,
        actuals_to_forecast=config.dashboard.actuals_to_forecast,
    )

    @ui.page("/")
    def render_dashboard_page() -> None:
        render_dashboard_styles()
        today = datetime.now(ZoneInfo(context.timezone_name)).date()
        layout = DashboardLayout(
            context,
            DashboardState(hourly_start=today, hourly_end=today),
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
