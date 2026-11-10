"""Operator actions for the dashboard Control tab."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nicegui import run, ui
from nicegui.events import GenericEventArguments

from solar_model.paths import subprocess_environment
from solar_model.config import Config
from solar_model.services.forecasting import backfill_daily_house_load_forecasts
from solar_model.storage.migrations import open_database

from solar_model.dashboard.components.warnings import notify_error, notify_info


def recalculation_command(start: date, end: date, config_path: Path) -> list[str]:
    """Build the non-shell command used by the tariff recalculation control."""
    return [
        sys.executable,
        "-m",
        "solar_model.tools.recalculate_economics",
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
        env=subprocess_environment(),
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


def load_holidays(database_path: str) -> list[tuple[str, str]]:
    database = open_database(database_path)
    try:
        return database.list_holiday_periods()
    finally:
        database.close()


def save_holidays(database_path: str, periods: list[tuple[str, str]]) -> list[tuple[str, str]]:
    database = open_database(database_path)
    try:
        database.replace_holiday_periods(periods)
        return database.list_holiday_periods()
    finally:
        database.close()


def holiday_periods_from_rows(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Convert edited rows to inclusive ranges, defaulting a missing date to one day."""
    def parse(value: str) -> date:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError("Holiday dates must use YYYY-MM-DD")
        return day

    periods = []
    for row in rows:
        start = str(row.get("start_date") or "").strip()
        end = str(row.get("end_date") or "").strip()
        if not start and not end:
            continue
        start_day = parse(start or end)
        end_day = parse(end or start)
        if end_day < start_day:
            raise ValueError("Holiday end date must not precede start date")
        periods.append((start_day.isoformat(), end_day.isoformat()))
    return periods


def backfill_load_months(config: Config, first: date, last: date) -> tuple[int, int]:
    database = open_database(config.database.path)
    try:
        return backfill_daily_house_load_forecasts(database, first, last, config.timezone, models=config.forecast.models)
    finally:
        database.close()


class ControlTab:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        # The dashboard and recalculation script both load config.yaml from
        # their working directory (the mounted /config directory in Docker).
        self.config_path = Path.cwd() / "config.yaml"
        self.holiday_grid = None

    def show_latest_holiday(self) -> None:
        if self.holiday_grid is not None and self.holiday_grid.options["rowData"]:
            self.holiday_grid.run_grid_method("ensureIndexVisible", len(self.holiday_grid.options["rowData"]) - 1, "bottom")

    def render_control_tab(self) -> None:
        def restart_with_feedback() -> None:
            try:
                restart_application()
            except (RuntimeError, OSError) as error:
                notify_error(f"Unable to restart application: {error}")

        today = datetime.now(self.timezone).date()
        year_start = one_year_before(today)
        ui.label("Control").classes("text-xl font-semibold")
        ui.label("Administrative actions update the shared database or restart the application.").classes("text-sm text-gray-600")

        with ui.card().classes("w-full max-w-xl mt-4"):
            ui.label("Holiday periods").classes("text-lg font-semibold")
            ui.label("Inclusive local dates use a separate holiday load profile. Changes affect new forecasts and the next Plan calculation; use Backfill below to replace saved days.").classes("text-sm text-gray-600")
            periods = load_holidays(self.config.database.path)
            rows = [{"id": index, "start_date": start, "end_date": end} for index, (start, end) in enumerate(periods)]
            grid = ui.aggrid({
                "columnDefs": [
                    {"field": "start_date", "headerName": "Start date", "editable": True, "cellDataType": "dateString", "cellEditor": "agDateStringCellEditor", "flex": 1},
                    {"field": "end_date", "headerName": "End date", "editable": True, "cellDataType": "dateString", "cellEditor": "agDateStringCellEditor", "flex": 1},
                    {"colId": "days", "headerName": "Days", "editable": False, "flex": .5, "minWidth": 65, "maxWidth": 100,
                     ":valueGetter": "p => { const start = Date.parse(p.data?.start_date); const end = Date.parse(p.data?.end_date); return Number.isFinite(start) && Number.isFinite(end) && end >= start ? (end - start) / 86400000 + 1 : null; }"},
                ],
                "rowData": rows,
                "rowSelection": "single",
                "stopEditingWhenCellsLoseFocus": True,
            }).classes("w-full h-64")
            self.holiday_grid = grid
            save_lock = asyncio.Lock()

            async def persist_holidays(rows_to_save: list[dict[str, Any]], focus_index: int | None = None) -> None:
                try:
                    periods = holiday_periods_from_rows(rows_to_save)
                    saved = await run.io_bound(save_holidays, self.config.database.path, periods)
                except Exception as error:
                    notify_error(f"Unable to save holidays: {error}")
                    return
                grid.options["rowData"] = [
                    {"id": index, "start_date": start, "end_date": end}
                    for index, (start, end) in enumerate(saved)
                ]
                grid.update()
                if focus_index is not None and saved:
                    index = min(focus_index, len(saved) - 1)
                    ui.timer(0.1, lambda: grid.run_grid_method("ensureIndexVisible", index, "bottom"), once=True)

            async def autosave_holidays(_event: GenericEventArguments) -> None:
                async with save_lock:
                    await persist_holidays(await grid.get_client_data(), _event.args.get("rowIndex"))

            grid.on("cellValueChanged", autosave_holidays)

            async def add_holiday() -> None:
                async with save_lock:
                    data = await grid.get_client_data()
                    next_id = max((row["id"] for row in data), default=-1) + 1
                    grid.options["rowData"] = [*data, {"id": next_id, "start_date": "", "end_date": ""}]
                    grid.update()
                    self.show_latest_holiday()

            async def delete_holiday() -> None:
                selected = await grid.get_selected_row()
                if selected is None:
                    notify_error("Select a holiday period to remove.")
                    return
                async with save_lock:
                    data = [row for row in await grid.get_client_data() if row["id"] != selected["id"]]
                    await persist_holidays(data)

            with ui.row().classes("gap-2 flex-wrap"):
                ui.button("Add period", on_click=add_holiday, icon="add").props("flat")
                ui.button("Remove selected", on_click=delete_holiday, icon="delete").props("flat")

            ui.separator().classes("my-2")
            ui.label("Backfill household-load forecasts").classes("text-lg font-semibold")
            ui.label("Holiday edits save automatically. Recalculate selected months using only readings before each day; existing forecasts in that range are replaced.").classes("text-sm text-gray-600")
            with ui.row().classes("gap-3 flex-wrap"):
                first_month = ui.input("From month", value=f"{year_start.year:04d}-{year_start.month:02d}").props("type=month outlined dense").classes("w-40")
                last_month = ui.input("To month", value=f"{today.year:04d}-{today.month:02d}").props("type=month outlined dense").classes("w-40")
            backfill_status = ui.label().classes("text-sm")

            async def backfill() -> None:
                try:
                    first = date.fromisoformat(f"{first_month.value}-01")
                    last_start = date.fromisoformat(f"{last_month.value}-01")
                    last = date(last_start.year, last_start.month, monthrange(last_start.year, last_start.month)[1])
                    last = min(last, today)
                    if first > last:
                        raise ValueError("From month must not be after To month or in the future")
                except ValueError as error:
                    notify_error(f"Backfill failed: {error}")
                    return
                backfill_button.set_enabled(False)
                backfill_status.set_text("Recalculating forecasts...")
                try:
                    days, hours = await run.io_bound(backfill_load_months, self.config, first, last)
                except Exception as error:
                    backfill_status.set_text("Backfill did not complete.")
                    notify_error(f"Backfill failed: {error}")
                else:
                    backfill_status.set_text(f"Recalculated {days} daily profiles ({hours} hourly rows).")
                finally:
                    backfill_button.set_enabled(True)

            with ui.dialog() as backfill_confirm, ui.card():
                ui.label("Backfill household-load forecasts?").classes("text-lg font-semibold")
                ui.label("Existing daily profiles in the selected months will be replaced in the database.")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=backfill_confirm.close).props("flat")

                    async def confirm_backfill() -> None:
                        backfill_confirm.close()
                        await backfill()

                    ui.button("Backfill", on_click=confirm_backfill, color="primary")
            backfill_button = ui.button("Backfill forecasts", on_click=backfill_confirm.open, icon="history", color="primary")

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
                    status.set_text("Recalculation did not complete.")
                    notify_error(f"Recalculation failed: {error}")
                    return

                if not os.environ.get("SOLAR_MODEL_RESTART_FILE"):
                    message = "Start the application with docker_start.py before using this control."
                    status.set_text("Recalculation did not complete.")
                    notify_error(message)
                    return

                running = True
                recalculation_button.set_visibility(False)
                progress.set_visibility(True)
                status.classes(replace="text-sm whitespace-pre-wrap")
                status.set_text("Recalculating tariff costs...")
                try:
                    output = await run.io_bound(run_recalculation, start, end, self.config_path)
                except (RuntimeError, subprocess.CalledProcessError) as error:
                    detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
                    status.set_text("Recalculation did not complete.")
                    notify_error(f"Recalculation failed: {detail}")
                    return
                finally:
                    running = False
                    progress.set_visibility(False)
                    recalculation_button.set_visibility(True)
                message = output or f"Recalculated tariff costs from {start} through {end}."
                status.set_text(f"{message}\nRestarting application to load the updated configuration...")
                status.classes(replace="text-sm whitespace-pre-wrap text-green-700")
                ui.timer(0.5, restart_with_feedback, once=True)

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
                        notify_info("Restarting application...")
                        ui.timer(0.5, restart_with_feedback, once=True)

                    ui.button("Restart", on_click=confirm_restart, color="negative")
            ui.button("Restart application", on_click=restart_confirm.open, icon="restart_alt", color="negative")
