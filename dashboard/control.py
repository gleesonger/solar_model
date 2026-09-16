"""Operator actions for the dashboard Control tab."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nicegui import run, ui

from common import source_path
from config import Config


def recalculation_command(start: date, end: date, config_path: Path) -> list[str]:
    """Build the non-shell command used by the tariff recalculation control."""
    return [
        sys.executable,
        str(source_path("recalculate_economics.py")),
        "--config",
        str(config_path),
        "--from",
        start.isoformat(),
        "--to",
        end.isoformat(),
    ]


def run_recalculation(start: date, end: date, config_path: Path) -> str:
    """Run the existing transactional recalculation script and return its output."""
    completed = subprocess.run(
        recalculation_command(start, end, config_path),
        cwd=config_path.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return (completed.stdout or completed.stderr).strip()


def restart_application() -> None:
    """Request a portable full restart from the application supervisor."""
    restart_file = os.environ.get("SOLAR_MODEL_RESTART_FILE")
    if not restart_file:
        raise RuntimeError("full application restart requires the managed application launcher")
    Path(restart_file).touch()


def one_year_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


class ControlTab:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        # The dashboard and recalculation script both load config.yaml from
        # their working directory (the mounted /config directory in Docker).
        self.config_path = Path.cwd() / "config.yaml"

    def render_control_tab(self) -> None:
        today = datetime.now(self.timezone).date()
        year_start = one_year_before(today)
        ui.label("Control").classes("text-xl font-semibold")
        ui.label("Administrative actions update the shared database or restart the application.").classes("text-sm text-gray-600")

        with ui.card().classes("w-full max-w-xl mt-4"):
            ui.label("Recalculate tariff costs").classes("text-lg font-semibold")
            ui.label("Reapply the configured tariff periods to stored samples in the selected inclusive date range.").classes("text-sm text-gray-600")
            with ui.row().classes("w-full gap-3"):
                start_input = ui.input("From", value=year_start.isoformat()).props(
                    f"type=date outlined dense max={today.isoformat()}"
                ).classes("w-40")
                end_input = ui.input("To", value=today.isoformat()).props(
                    f"type=date outlined dense max={today.isoformat()}"
                ).classes("w-40")
            status = ui.label().classes("text-sm whitespace-pre-wrap")
            running = False

            async def recalculate() -> None:
                nonlocal running
                if running:
                    return
                try:
                    start = date.fromisoformat(str(start_input.value))
                    end = date.fromisoformat(str(end_input.value))
                    if start > end:
                        raise ValueError("From date must not be after To date")
                    if end > today:
                        raise ValueError("To date cannot be in the future")
                except ValueError as error:
                    status.set_text(f"Recalculation failed: {error}")
                    status.classes(replace="text-sm whitespace-pre-wrap text-red-600")
                    ui.notify(f"Recalculation failed: {error}", type="negative")
                    return

                if not os.environ.get("SOLAR_MODEL_RESTART_FILE"):
                    message = "Start the application with docker_start.py before using this control."
                    status.set_text(f"Recalculation failed: {message}")
                    status.classes(replace="text-sm whitespace-pre-wrap text-red-600")
                    ui.notify(message, type="negative")
                    return

                running = True
                recalculation_button.set_visibility(False)
                progress.set_visibility(True)
                status.set_text("Recalculating tariff costs...")
                try:
                    output = await run.io_bound(run_recalculation, start, end, self.config_path)
                except (RuntimeError, subprocess.CalledProcessError) as error:
                    detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
                    status.set_text(f"Recalculation failed: {detail}")
                    status.classes(replace="text-sm whitespace-pre-wrap text-red-600")
                    ui.notify(f"Recalculation failed: {detail}", type="negative")
                    return
                finally:
                    running = False
                    progress.set_visibility(False)
                    recalculation_button.set_visibility(True)
                message = output or f"Recalculated tariff costs from {start} through {end}."
                status.set_text(f"{message}\nRestarting application to load the updated configuration...")
                status.classes(replace="text-sm whitespace-pre-wrap text-green-700")
                ui.timer(0.5, restart_application, once=True)

            with ui.dialog() as recalculation_confirm, ui.card().classes("w-96"):
                ui.label("Recalculate tariff costs?").classes("text-lg font-semibold")
                ui.label("This overwrites stored tariff rates and cost calculations for the selected period.")
                ui.label("The entire application will restart afterwards so the dashboard and collector use the updated configuration.").classes("text-sm text-gray-600")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=recalculation_confirm.close).props("flat")

                    async def confirm_recalculation() -> None:
                        recalculation_confirm.close()
                        await recalculate()

                    ui.button("Recalculate", on_click=confirm_recalculation, color="primary")
            with ui.row().classes("items-center gap-2"):
                recalculation_button = ui.button(
                    "Recalculate tariff costs",
                    on_click=recalculation_confirm.open,
                    icon="calculate",
                    color="primary",
                )
                with ui.row().classes("items-center gap-2 text-sm text-gray-600") as progress:
                    ui.spinner("dots", size="md")
                    ui.label("Recalculating tariff costs; the application will restart when complete...")
                progress.set_visibility(False)

        with ui.card().classes("w-full max-w-xl mt-4"):
            ui.label("Restart application").classes("text-lg font-semibold")
            ui.label("Reloads configuration and restarts both the dashboard and data collector.").classes("text-sm text-gray-600")
            with ui.dialog() as restart_confirm, ui.card().classes("w-96"):
                ui.label("Restart application?").classes("text-lg font-semibold")
                ui.label("Connected browsers will briefly disconnect and then reload.")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=restart_confirm.close).props("flat")

                    def confirm_restart() -> None:
                        restart_confirm.close()
                        ui.notify("Restarting application...", type="info")
                        ui.timer(0.5, restart_application, once=True)

                    ui.button("Restart", on_click=confirm_restart, color="negative")
            ui.button("Restart application", on_click=restart_confirm.open, icon="restart_alt", color="negative")
