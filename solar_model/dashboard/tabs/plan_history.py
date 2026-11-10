"""Read-only browsing of committed plan snapshots, without running the model."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nicegui import run, ui

from solar_model.config import Config
from solar_model.dashboard.components.plan_detail import detail_excel, render_detail_table
from solar_model.dashboard.components.warnings import notify_error
from solar_model.planning.types import OperatingMode
from solar_model.services.plan_history import payload_frame
from solar_model.storage.plan_history import StoredPlan, list_plans, load_plan


class PlanHistoryTab:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        self.snapshots: list[StoredPlan] = []
        self.current: StoredPlan | None = None
        self.loaded = False
        self.busy = False
        self._request = 0

    def render(self) -> None:
        with ui.column().classes("w-full min-w-0 gap-3"):
            with ui.row().classes("w-full items-end flex-wrap gap-2"):
                self.day = ui.input("Date", value=datetime.now(self.timezone).date().isoformat()).props("type=date outlined dense").classes("w-40")
                ui.button("Load date", on_click=lambda: self.refresh()).props("outline dense")
                ui.button("Latest", on_click=self.latest).props("outline dense")
                ui.button("Previous", on_click=lambda: self.navigate(-1)).props("outline dense")
                self.selection = ui.select({}, label="Snapshot", on_change=self.select_snapshot).props("outlined dense").classes("w-72 max-w-full")
                ui.button("Next", on_click=lambda: self.navigate(1)).props("outline dense")
            with ui.row().classes("w-full items-center flex-wrap gap-3"):
                self.solar = ui.select(["low", "med", "high"], value="med", label="Solar", on_change=self.show_details).props("outlined dense").classes("w-28")
                self.load = ui.select(["low", "med", "high"], value="med", label="House load", on_change=self.show_details).props("outlined dense").classes("w-32")
                self.view = ui.toggle(["Compact", "Advanced"], value="Compact", on_change=self.show_details).props("dense no-caps")
                self.download = ui.button("Download Excel", icon="download", on_click=self.export).props("flat dense")
            self.progress = ui.spinner(size="sm")
            self.progress.set_visibility(False)
            self.status = ui.label("Open Plan History to load saved plans.").classes("text-sm text-gray-600")
            self.output = ui.column().classes("w-full min-w-0")
        self.loaded = True

    async def activate(self) -> None:
        if self.loaded and not self.busy:
            if self.current is None:
                await self.latest()
            else:
                await self.refresh()

    async def latest(self) -> None:
        await self.refresh(use_latest=True)

    async def refresh(self, use_latest: bool = False) -> None:
        if self.busy:
            return
        self.busy = True
        self._request += 1
        self.progress.set_visibility(True)
        try:
            preferred = self.selection.value
            if use_latest:
                newest = await run.io_bound(load_plan, self.config.database.path)
                if newest is None:
                    self.snapshots = []
                    self.selection.set_options({}, value=None)
                    self.current = None
                    self.show_details()
                    return
                self.day.set_value(newest.saved_at.astimezone(self.timezone).date().isoformat())
                preferred = newest.snapshot_id
            day = date.fromisoformat(self.day.value)
            start = datetime.combine(day, time.min, self.timezone)
            end = datetime.combine(day + timedelta(days=1), time.min, self.timezone)
            self.snapshots = await run.io_bound(list_plans, self.config.database.path, start, end)
            options = {plan.snapshot_id: plan.saved_at.astimezone(self.timezone).strftime("%H:%M:%S %Z") for plan in self.snapshots}
            selected = preferred if preferred in options else next(iter(options), None)
            self.selection.set_options(options, value=selected)
            if selected is None:
                self.current = None
                self.show_details()
            else:
                await self.select_snapshot()
        except Exception as error:
            self.current = None
            self.output.clear()
            self.status.set_text(f"Unable to load plan history: {error}")
            notify_error(f"Unable to load plan history: {error}")
        finally:
            self.busy = False
            self.progress.set_visibility(False)

    async def select_snapshot(self) -> None:
        snapshot_id = self.selection.value
        if snapshot_id is None:
            return
        self._request += 1
        request = self._request
        self.progress.set_visibility(True)
        try:
            saved = await run.io_bound(load_plan, self.config.database.path, snapshot_id)
            if request == self._request:
                self.current = saved
                self.show_details()
        except Exception as error:
            if request == self._request:
                self.current = None
                self.output.clear()
                self.status.set_text(f"Unable to load snapshot: {error}")
        finally:
            if request == self._request:
                self.progress.set_visibility(False)

    async def navigate(self, direction: int) -> None:
        # List order is newest first; Previous means an older calculation.
        ids = [plan.snapshot_id for plan in self.snapshots]
        if self.selection.value in ids:
            index = ids.index(self.selection.value) - direction
            if 0 <= index < len(ids):
                self.selection.set_value(ids[index])

    def show_details(self) -> None:
        self.output.clear()
        self.download.disable()
        if self.current is None:
            self.status.set_text("No saved plans for this date. Plans appear after a successful background calculation.")
            return
        metadata = self.current.metadata
        stamp = self.current.saved_at.astimezone(self.timezone)
        source = datetime.fromisoformat(metadata["full_data_at"]).astimezone(self.timezone)
        self.status.set_text(f"Calculated {stamp:%Y-%m-%d %H:%M:%S %Z} | Model: {metadata['model']} | Source reading: {source:%Y-%m-%d %H:%M:%S %Z}")
        scenario = "med" if self.load.value == self.solar.value == "med" else f"{self.load.value}_{self.solar.value}"
        payload = self.current.sections.get((self.view.value, scenario))
        with self.output:
            if payload is None:
                ui.label("Advanced details have expired for this snapshot. Select Compact." if self.view.value == "Advanced" else "This scenario is unavailable in the saved plan.").classes("text-sm text-gray-600")
                return
            if metadata.get("expected_score") is not None:
                ui.label("Fixed-condition stress illustration; not the Markov expectation.").classes("text-sm text-gray-600")
            frame = payload_frame(payload)
            render_detail_table(frame, metadata["timezone"], OperatingMode(metadata["action"]["mode"]), self.view.value)
        self.download.enable()

    def export(self) -> None:
        if self.current is None:
            return
        scenario = "med" if self.load.value == self.solar.value == "med" else f"{self.load.value}_{self.solar.value}"
        payload = self.current.sections.get((self.view.value, scenario))
        if payload is None:
            return
        try:
            metadata = self.current.metadata
            content = detail_excel(payload_frame(payload), metadata["timezone"], OperatingMode(metadata["action"]["mode"]), self.view.value)
            stamp = self.current.saved_at.astimezone(self.timezone)
            ui.download(content, filename=f"plan-history-{stamp:%Y%m%d-%H%M%S}-{scenario}-{self.view.value.lower()}.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as error:
            notify_error(f"Unable to export saved plan: {error}")
