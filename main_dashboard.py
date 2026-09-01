from __future__ import annotations

from pathlib import Path

from nicegui import app, ui

from config import load_config
from database import open_database
from dashboard.layout import DashboardLayout, render_dashboard_styles
from dashboard.live import LivePowerCollector
from common import LOGGER, configure_logging, source_path

def main() -> None:
    config = load_config()
    configure_logging(config.logging.level)
    database = open_database(config.database.path)
    database.close()
    LOGGER.info("Dashboard server starting on %s:%s", config.dashboard.host, config.dashboard.port)

    live_collector = LivePowerCollector(
        config.actuals.modbus,
        config.timezone,
        config.database.path,
        config.dashboard.actuals_to_forecast,
        config.actuals.data_retrival_schedule.live_update_interval_seconds,
    )
    app.on_shutdown(live_collector.stop)

    @ui.page("/")
    def render_dashboard_page() -> None:
        client = ui.context.client

        def refresh_browser_activity(event: str) -> None:
            LOGGER.info(f"Dashboard browser {event}: {client.id}")
            live_collector.refresh_browser_activity()

        client.on_connect(lambda: refresh_browser_activity("connected"))
        client.on_disconnect(lambda: refresh_browser_activity("disconnected"))
        render_dashboard_styles()
        layout = DashboardLayout(
            config,
            live_collector,
        )
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            layout.render_dashboard_layout(container)
        ui.timer(
            config.actuals.data_retrival_schedule.live_update_interval_seconds,
            layout.refresh_live_power,
        )
        ui.timer(
            config.actuals.data_retrival_schedule.full_updated_interval_seconds,
            layout.refresh_dashboard,
        )

    ui.run(
        host=config.dashboard.host,
        port=config.dashboard.port,
        title="Solar dashboard",
        favicon=source_path("dashboard/assets/solar_power.svg"),
        reload=False,
    )


if __name__ == "__main__":
    main()
