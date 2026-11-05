from __future__ import annotations


from nicegui import app, background_tasks, ui
from nicegui.client import Client

from solar_model.config import load_config
from solar_model.storage.migrations import open_database
from solar_model.dashboard.layout import DashboardLayout, render_dashboard_styles
from solar_model.dashboard.live import LivePowerCollector
from solar_model.dashboard.snapshot import DashboardSnapshotStore
from solar_model.logging_setup import LOGGER, configure_logging
from solar_model.paths import package_path
from solar_model.dashboard.planning_worker import ensure_planning_worker
from solar_model.dashboard.plan_store import PlanStore

def main(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    configure_logging(config.logging.level)
    database = open_database(config.database.path)
    database.close()
    LOGGER.info("Dashboard server starting on %s:%s", config.dashboard.host, config.dashboard.port)

    live_collector = LivePowerCollector(
        config.actuals.modbus,
        config.timezone,
        config.database.path,
        config.actuals.data_retrival_schedule.live_update_interval_seconds,
        tuple(config.dashboard.actuals_to_forecast),
    )
    snapshots = DashboardSnapshotStore(
        config,
        live_collector,
        config.actuals.data_retrival_schedule.full_updated_interval_seconds,
    )
    plans = PlanStore(config, config_path)
    app.on_shutdown(live_collector.stop)
    app.on_shutdown(snapshots.stop)
    app.on_shutdown(plans.stop)

    async def start_snapshot_worker() -> None:
        # Load on demand and pause summaries when no browser is subscribed.
        background_tasks.create(snapshots.run_forever(), name="dashboard snapshot worker")

    app.on_startup(ensure_planning_worker)
    app.on_startup(start_snapshot_worker)

    async def start_plan_worker() -> None:
        background_tasks.create(plans.run_forever(), name="background plan updates")

    app.on_startup(start_plan_worker)

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
            config_path=config_path,
            plan_store=plans,
        )
        snapshot = snapshots.current()
        layout.historical_tab.render_mobile_historical_controls()
        with ui.column().classes("w-full max-w-7xl mx-auto p-2 md:p-4") as container:
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

        unsubscribe = lambda: None

        def apply_plan() -> None:
            client.safe_invoke(layout.plan_tab.apply_shared_plan)

        unsubscribe_plan = lambda: None

        def resume_updates() -> None:
            nonlocal unsubscribe, unsubscribe_plan
            unsubscribe()
            unsubscribe = snapshots.subscribe(apply_snapshot)
            unsubscribe_plan()
            unsubscribe_plan = plans.subscribe(apply_plan)
            latest_snapshot = snapshots.current()
            if latest_snapshot is not None:
                apply_snapshot(latest_snapshot)
            apply_plan()

        client.on_connect(resume_updates)
        client.on_disconnect(lambda: unsubscribe())
        client.on_disconnect(lambda: unsubscribe_plan())
        ui.timer(
            config.actuals.data_retrival_schedule.live_update_interval_seconds,
            layout.refresh_live_power,
        )

    # The explicit page owns per-browser outboxes; the implicit page is unused.
    Client.auto_index_client.outbox.stop()
    ui.run(
        host=config.dashboard.host,
        port=config.dashboard.port,
        title="Solar dashboard",
        favicon=package_path("dashboard/assets/solar_power.svg"),
        reload=False,
        binding_refresh_interval=0.25,
    )


if __name__ == "__main__":
    main()
