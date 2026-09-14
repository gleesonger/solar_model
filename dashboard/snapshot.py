"""Shared, server-side dashboard data cache.

The dashboard has client-specific NiceGUI elements, but its database summaries
are identical for every browser.  This module loads those summaries once and
publishes immutable snapshot references to connected pages.
"""

from __future__ import annotations

import asyncio
from time import perf_counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
from nicegui import run

from common import LOGGER, heavy_work
from config import Config

from . import data
from .live import LivePowerCollector
from .models import LivePowerData, TelemetryData


RecentData = tuple[pd.DataFrame, TelemetryData, LivePowerData, pd.DataFrame, pd.DataFrame]


@dataclass(frozen=True)
class DashboardSnapshot:
    version: int
    loaded_at: datetime
    recent_data: RecentData
    device_information: list[dict[str, str]]


@heavy_work("dashboard shared Recent snapshot data")
def load_recent_data(config: Config, live_collector: LivePowerCollector) -> RecentData:
    """Load the data common to every browser's Recent tab."""
    timezone = ZoneInfo(config.timezone)
    today = datetime.now(timezone).date()
    telemetry = data.load_telemetry(config.database.path, config.timezone)
    return (
        data.load_day(
            config.database.path,
            config.timezone,
            config.forecast.arrays,
            config.dashboard.actuals_to_forecast,
        ).copy(),
        telemetry,
        live_collector.snapshot(),
        data.load_recent_daily_energy(
            config.database.path,
            config.timezone,
            today,
            7,
            config.forecast.arrays,
        ),
        data.load_recent_monthly_energy(
            config.database.path,
            config.timezone,
            today,
            12,
            config.forecast.arrays,
        ),
    )


class DashboardSnapshotStore:
    """Refresh shared data once per interval and notify subscribed pages."""

    def __init__(self, config: Config, live_collector: LivePowerCollector, interval_seconds: float) -> None:
        self._config = config
        self._live_collector = live_collector
        self._interval_seconds = interval_seconds
        self._lock = Lock()
        self._snapshot: DashboardSnapshot | None = None
        self._listeners: dict[int, Callable[[DashboardSnapshot], None]] = {}
        self._next_listener_id = 0
        self._stop = asyncio.Event()

    def current(self) -> DashboardSnapshot | None:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: Callable[[DashboardSnapshot], None]) -> Callable[[], None]:
        with self._lock:
            listener_id = self._next_listener_id
            self._next_listener_id += 1
            self._listeners[listener_id] = listener

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.pop(listener_id, None)

        return unsubscribe

    async def refresh(self) -> DashboardSnapshot | None:
        """Load a complete replacement snapshot without blocking NiceGUI's loop."""
        started_at = perf_counter()
        LOGGER.info("Heavy work started: dashboard full shared snapshot refresh")
        try:
            recent_data, device_information = await asyncio.gather(
                run.io_bound(load_recent_data, self._config, self._live_collector),
                run.io_bound(data.load_device_information, self._config.database.path),
            )
        except Exception:
            LOGGER.exception(
                "Heavy work failed: dashboard full shared snapshot refresh (%.3fs)",
                perf_counter() - started_at,
            )
            return None

        with self._lock:
            previous_version = self._snapshot.version if self._snapshot is not None else 0
            snapshot = DashboardSnapshot(
                version=previous_version + 1,
                loaded_at=datetime.now(),
                recent_data=recent_data,
                device_information=device_information,
            )
            self._snapshot = snapshot
            listeners = tuple(self._listeners.values())

        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                LOGGER.exception("Dashboard snapshot listener failed")
        LOGGER.info(
            "Heavy work completed: dashboard full shared snapshot refresh "
            "(version %s, %.3fs)",
            snapshot.version,
            perf_counter() - started_at,
        )
        return snapshot

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                await self.refresh()

    def stop(self) -> None:
        self._stop.set()
