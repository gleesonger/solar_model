"""In-memory live Modbus readings for the dashboard server."""

from __future__ import annotations

from threading import Event, Lock, Thread

from nicegui.client import Client
from pymodbus.client import ModbusTcpClient

from common import LOGGER, timestamps
from config import ModbusConfig
from modbus_collector import load_registers, read_registers

from .models import LivePowerData, SUMMARY_LATEST_KEYS


LIVE_POWER_METRICS = {
    "plant_grid_power_kw",
    "plant_pv_power_kw",
    "plant_battery_power_kw",
    "plant_load_power_kw",
    "inverter_power_kw",
}
LIVE_BATTERY_METRICS = {
    "inverter_battery_available_discharge",
    "plant_battery_soc",
}
LIVE_TODAY_METRICS = {
    "plant_pv_daily_kwh",
    "plant_load_daily_kwh",
    "inverter_battery_charge_daily_kwh",
    "inverter_battery_discharge_daily_kwh",
    "inverter_pv_daily_kwh",
}
LIVE_VALUE_KEYS = {
    "plant_grid_power_kw": "grid",
    "plant_pv_power_kw": "solar",
    "plant_battery_power_kw": "battery",
    "plant_load_power_kw": "load",
    "inverter_power_kw": "inverter",
}
LIVE_COLLECTION_INTERVAL_SECONDS = 1


class LivePowerCollector:
    """Collect the dashboard's latest power readings without writing to SQLite."""

    def __init__(
        self,
        modbus: ModbusConfig,
        timezone_name: str,
        actuals_to_forecast: dict[str, int],
    ) -> None:
        self._modbus = modbus
        self._timezone_name = timezone_name
        self._actuals_to_forecast = actuals_to_forecast
        self._registers = load_registers(
            modbus.register_map,
            modbus.default_device_id,
            metrics=self._metrics(),
        )
        self._lock = Lock()
        self._stop_event = Event()
        self._active_event = Event()
        self._thread: Thread | None = None
        self._latest = LivePowerData(None, None, self._empty_values())
        self._has_received_data = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="live-power-collector", daemon=True)
        self._thread.start()
        LOGGER.info("Live collector thread started")

    def stop(self) -> None:
        if self._thread is None:
            return
        LOGGER.info("Live collector stopping")
        self._stop_event.set()
        self._active_event.set()
        self._thread.join(timeout=self._modbus.timeout_seconds + 1)
        LOGGER.info("Live collector stopped")

    def refresh_browser_activity(self) -> None:
        has_connected_browser = any(
            client.has_socket_connection
            for client in Client.instances.values()
        )
        with self._lock:
            was_active = self._active_event.is_set()
            if has_connected_browser:
                self._active_event.set()
            else:
                self._active_event.clear()
        if has_connected_browser and not was_active:
            LOGGER.info("Live collector activated by browser connection")
        if not has_connected_browser and was_active:
            LOGGER.info("Live collector paused: no browser connected")
        if has_connected_browser:
            self.start()

    def snapshot(self) -> LivePowerData:
        with self._lock:
            return LivePowerData(
                collected_at_utc=self._latest.collected_at_utc,
                collected_at_local=self._latest.collected_at_local,
                values=self._latest.values.copy(),
            )

    def _metrics(self) -> set[str]:
        metrics = LIVE_POWER_METRICS | LIVE_BATTERY_METRICS | LIVE_TODAY_METRICS
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
        values["battery_available_energy_kwh"] = None
        values["battery_soc_percent"] = None
        values.update({
            "today_solar": None,
            "today_load": None,
            "today_battery_charge": None,
            "today_battery_discharge": None,
            "today_inverter": None,
        })
        values.update({
            f"actual_panel_{panel_id}": None
            for panel_id in self._actuals_to_forecast.values()
        })
        return values

    def _run(self) -> None:
        client: ModbusTcpClient | None = None
        try:
            while not self._stop_event.is_set():
                if not self._active_event.is_set():
                    if client is not None:
                        client.close()
                        client = None
                    self._active_event.wait()
                    continue
                if client is None:
                    client = self._connect()
                if client is not None and not self._collect_once(client):
                    client.close()
                    client = None
                self._stop_event.wait(LIVE_COLLECTION_INTERVAL_SECONDS)
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
        LOGGER.warning("Live Modbus connection failed; retrying next cycle")
        return None

    def _collect_once(self, client: ModbusTcpClient) -> bool:
        try:
            readings = read_registers(client, self._registers)
            values = self._power_values(readings)
            collected_at_utc, collected_at_local = timestamps(timezone_name=self._timezone_name)
            with self._lock:
                self._latest = LivePowerData(collected_at_utc, collected_at_local, values)
                first_successful_read = not self._has_received_data
                self._has_received_data = True
            if first_successful_read:
                LOGGER.info("Live collector received first successful reading")
            return True
        except Exception:
            LOGGER.warning("Live Modbus interval failed; interval skipped")
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

        available_energy, _, _ = readings["inverter_battery_available_discharge"]
        soc_percent, _, _ = readings["plant_battery_soc"]
        if isinstance(available_energy, str) or isinstance(soc_percent, str):
            raise ValueError("live battery readings returned text")
        values["battery_available_energy_kwh"] = available_energy
        values["battery_soc_percent"] = soc_percent

        today_metrics = {
            "today_solar": "plant_pv_daily_kwh",
            "today_load": "plant_load_daily_kwh",
            "today_battery_charge": "inverter_battery_charge_daily_kwh",
            "today_battery_discharge": "inverter_battery_discharge_daily_kwh",
            "today_inverter": "inverter_pv_daily_kwh",
        }
        for key, metric in today_metrics.items():
            value, _, _ = readings[metric]
            if isinstance(value, str):
                raise ValueError(f"live daily metric {metric!r} returned text")
            values[key] = value

        for pv_string, panel_id in self._actuals_to_forecast.items():
            voltage, _, _ = readings[f"inverter_{pv_string}_voltage"]
            current, _, _ = readings[f"inverter_{pv_string}_current"]
            if isinstance(voltage, str) or isinstance(current, str):
                raise ValueError(f"live PV string {pv_string!r} returned text")
            values[f"actual_panel_{panel_id}"] = max(voltage * current, 0.0) / 1000
        return values
