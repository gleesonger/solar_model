"""In-memory live Modbus readings for the dashboard server."""

from __future__ import annotations

from threading import Event, Lock, Thread

from pymodbus.client import ModbusTcpClient

from common import LOGGER, timestamps
from config import ModbusConfig
from modbus_collector import RegisterBlock, load_registers, read_registers

from .models import LivePowerData, SUMMARY_LATEST_KEYS


LIVE_POWER_METRICS = {
    "plant_grid_power_kw",
    "plant_pv_power_kw",
    "plant_battery_power_kw",
    "plant_load_power_kw",
    "inverter_power_kw",
}
LIVE_VALUE_KEYS = {
    "plant_grid_power_kw": "grid",
    "plant_pv_power_kw": "solar",
    "plant_battery_power_kw": "battery",
    "plant_load_power_kw": "load",
    "inverter_power_kw": "inverter",
}


class LivePowerCollector:
    """Collect the dashboard's latest power readings without writing to SQLite."""

    def __init__(
        self,
        modbus: ModbusConfig,
        timezone_name: str,
        interval_seconds: int,
        actuals_to_forecast: dict[str, int],
    ) -> None:
        self._modbus = modbus
        self._timezone_name = timezone_name
        self._interval_seconds = interval_seconds
        self._actuals_to_forecast = actuals_to_forecast
        self._registers = load_registers(
            modbus.register_map,
            modbus.default_device_id,
            metrics=self._metrics(),
        )
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._latest = LivePowerData(None, None, self._empty_values())

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="live-power-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._modbus.timeout_seconds + 1)

    def snapshot(self) -> LivePowerData:
        with self._lock:
            return LivePowerData(
                collected_at_utc=self._latest.collected_at_utc,
                collected_at_local=self._latest.collected_at_local,
                values=self._latest.values.copy(),
            )

    def _metrics(self) -> set[str]:
        metrics = set(LIVE_POWER_METRICS)
        for pv_string in self._actuals_to_forecast:
            metrics.update({
                f"inverter_{pv_string}_voltage",
                f"inverter_{pv_string}_current",
            })
        return metrics

    def _empty_values(self) -> dict[str, float | None]:
        values: dict[str, float | None] = {
            key: None for key in SUMMARY_LATEST_KEYS.values()
        }
        values.update({
            f"actual_panel_{panel_id}": None
            for panel_id in self._actuals_to_forecast.values()
        })
        return values

    def _run(self) -> None:
        client: ModbusTcpClient | None = None
        try:
            while not self._stop_event.is_set():
                if client is None:
                    client = self._connect()
                if client is not None and not self._collect_once(client):
                    client.close()
                    client = None
                self._stop_event.wait(self._interval_seconds)
        finally:
            if client is not None:
                client.close()

    def _connect(self) -> ModbusTcpClient | None:
        try:
            client = ModbusTcpClient(
                self._modbus.host,
                port=self._modbus.port,
                timeout=self._modbus.timeout_seconds,
            )
            if client.connect():
                return client
            client.close()
        except Exception:
            pass
        LOGGER.info("Live Modbus connection failed; retrying next cycle")
        return None

    def _collect_once(self, client: ModbusTcpClient) -> bool:
        try:
            readings = read_registers(client, self._registers)
            values = self._power_values(readings)
            collected_at_utc, collected_at_local = timestamps(timezone_name=self._timezone_name)
            with self._lock:
                self._latest = LivePowerData(collected_at_utc, collected_at_local, values)
            return True
        except Exception:
            LOGGER.info("Live Modbus interval failed; interval skipped")
            return False

    def _power_values(
        self,
        readings: dict[str, tuple[float | str, str, bool]],
    ) -> dict[str, float | None]:
        values = self._empty_values()
        for metric, key in LIVE_VALUE_KEYS.items():
            value, _, _ = readings[metric]
            if isinstance(value, str):
                raise ValueError(f"live electrical metric {metric!r} returned text")
            values[key] = value

        grid_power = values["grid"]
        values["grid_import"] = max(grid_power, 0.0) if grid_power is not None else None
        values["grid_export"] = max(-grid_power, 0.0) if grid_power is not None else None

        for pv_string, panel_id in self._actuals_to_forecast.items():
            voltage, _, _ = readings[f"inverter_{pv_string}_voltage"]
            current, _, _ = readings[f"inverter_{pv_string}_current"]
            if isinstance(voltage, str) or isinstance(current, str):
                raise ValueError(f"live PV string {pv_string!r} returned text")
            values[f"actual_panel_{panel_id}"] = max(voltage * current, 0.0) / 1000
        return values
