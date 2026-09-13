from __future__ import annotations

from pathlib import Path

from nicegui import app, background_tasks, ui

from config import load_config
from database import open_database
from dashboard.layout import DashboardLayout, render_dashboard_styles
from dashboard.live import LivePowerCollector
from dashboard.snapshot import DashboardSnapshotStore
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
        config.actuals.data_retrival_schedule.live_update_interval_seconds,
    )
    snapshots = DashboardSnapshotStore(
        config,
        live_collector,
        config.actuals.data_retrival_schedule.full_updated_interval_seconds,
    )
    app.on_shutdown(live_collector.stop)
    app.on_shutdown(snapshots.stop)

    async def start_snapshot_worker() -> None:
        # Load once before accepting browser pages, so first render uses cached
        # data rather than blocking every connecting client on database work.
        await snapshots.refresh()
        background_tasks.create(snapshots.run_forever(), name="dashboard snapshot worker")

    app.on_startup(start_snapshot_worker)

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
        snapshot = snapshots.current()
        layout.historical_tab.render_mobile_historical_controls()
        with ui.column().classes("w-full max-w-7xl mx-auto p-4") as container:
            if snapshot is None:
                ui.spinner("dots", size="lg")
                ui.label("Dashboard data is loading…")
            else:
                layout.render_dashboard_layout(container, snapshot)
        has_rendered_snapshot = snapshot is not None

        def apply_snapshot(updated_snapshot) -> None:
            def update_client() -> None:
                nonlocal has_rendered_snapshot
                if has_rendered_snapshot:
                    layout.apply_snapshot(updated_snapshot)
                    return
                container.clear()
                layout.render_dashboard_layout(container, updated_snapshot)
                has_rendered_snapshot = True

            client.safe_invoke(update_client)

        unsubscribe = snapshots.subscribe(apply_snapshot)
        client.on_disconnect(unsubscribe)
        ui.timer(
            config.actuals.data_retrival_schedule.live_update_interval_seconds,
            layout.refresh_live_power,
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
