from __future__ import annotations

import time

import requests
from pymodbus.client import ModbusTcpClient

from common import LOGGER, configure_logging, sleep_until_next_interval
from config import load_config
from database import open_database
from forecast_collector import collect_once as collect_forecast_once
from modbus_collector import collect_once as collect_modbus_once, load_registers


def main() -> None:
    config = load_config()
    scheduler = config.actuals.scheduler
    modbus = config.actuals.modbus
    configure_logging(config.logging.level)
    registers = load_registers(modbus.register_map, modbus.default_device_id)
    device_registers = load_registers(modbus.device_register_map, modbus.default_device_id)
    database = open_database(config.database.path)
    client = None
    previous = database.load_previous_cumulatives()

    try:
        with requests.Session() as session:
            LOGGER.info("Waiting for next interval before starting collectors")
            sleep_until_next_interval(scheduler.interval_seconds)
            last_forecast_scan: float | None = None
            while True:
                if client is None:
                    try:
                        candidate = ModbusTcpClient(modbus.host, port=modbus.port, timeout=modbus.timeout_seconds)
                        if candidate.connect():
                            client = candidate
                        else:
                            candidate.close()
                            LOGGER.info("Modbus connection failed; retrying next cycle")
                    except Exception:
                        LOGGER.info("Modbus connection failed; retrying next cycle")
                if client is not None and not collect_modbus_once(client,database,registers,previous,config.timezone,device_registers):
                    client.close()
                    client = None

                now = time.monotonic()

                if last_forecast_scan is None or now - last_forecast_scan >= config.forecast.interval_seconds:
                    collect_forecast_once(session, database, config.forecast.arrays, config.forecast.endpoint, config.forecast.timeout_seconds, config.timezone)
                    last_forecast_scan = now
                sleep_until_next_interval(scheduler.interval_seconds)
    finally:
        if client is not None:
            client.close()
        database.close()
        LOGGER.info("collectors stopped")


if __name__ == "__main__":
    main()
