"""Dashboard warning policy and presentation.

Ongoing connection/data problems use one browser-owned banner which clears only
when healthy. Action failures use dismissible notifications with shared defaults.
Tabs publish timestamps; only warnings.js evaluates and displays freshness.
"""

from pathlib import Path
from typing import Literal

from nicegui import ui

from config import Config

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 20


def _notify(message: str, kind: Literal["negative", "info", "positive"]) -> None:
    ui.notify(message, type=kind, position="top", timeout=10000 if kind == "negative" else 5000,
              close_button="Dismiss", multi_line=True)


def notify_error(message: str) -> None:
    _notify(message, "negative")


def notify_info(message: str) -> None:
    _notify(message, "info")


def notify_success(message: str) -> None:
    _notify(message, "positive")


class DashboardWarnings:
    """One per browser page, outside containers that are rebuilt on refresh."""

    def __init__(self, config: Config) -> None:
        schedule = config.actuals.data_retrival_schedule
        self.metadata = ui.element("div").props(
            'id="dashboard-warning-data" hidden '
            f'data-full-stale-after-seconds="{schedule.full_updated_interval_seconds * 3}" '
            f'data-live-stale-after-seconds="{max(15, schedule.live_update_interval_seconds * 3)}" '
            f'data-heartbeat-timeout-ms="{HEARTBEAT_TIMEOUT_SECONDS * 1000}"'
        )
        ui.add_body_html('''
            <div id="dashboard-warning-banner" role="alert"
                 style="position:fixed;bottom:0;left:0;right:0;z-index:1999;
                        background:#991b1b;color:white;padding:12px 16px;
                        padding-bottom:max(12px,env(safe-area-inset-bottom));
                        font-weight:600;box-shadow:0 -2px 8px #0003">
                <span id="dashboard-warning-message">Checking connection — displayed data may be stale.</span>
                <button type="button" onclick="window.location.reload()"
                        style="margin-left:12px;text-decoration:underline">Reload</button>
            </div>
        ''')
        script = Path(__file__).with_name("assets").joinpath("warnings.js").read_text(encoding="utf-8")
        ui.add_head_html(f"<script>{script}</script>")
        client = ui.context.client
        heartbeat_started = False

        def heartbeat() -> None:
            if client.has_socket_connection:
                client.run_javascript('window.dashboardHeartbeat?.()')

        def start_heartbeat() -> None:
            nonlocal heartbeat_started
            heartbeat()
            if not heartbeat_started:
                with client:
                    ui.timer(HEARTBEAT_INTERVAL_SECONDS, heartbeat, immediate=False)
                heartbeat_started = True

        client.on_connect(start_heartbeat)

    def update_full(self, timestamp: str | None) -> None:
        self.metadata.props(f'data-last-full-update="{timestamp or ""}"')

    def update_live(self, timestamp: str | None) -> None:
        self.metadata.props(f'data-last-live-update="{timestamp or ""}"')
