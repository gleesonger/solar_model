from __future__ import annotations

import logging
import time

import requests
from pymodbus.client import ModbusTcpClient

from common import LOGGER, sleep_until_next_interval
from config import load_config
from database import open_database
from forecast_collector import collect_once as collect_forecast_once
from modbus_collector import collect_once as collect_modbus_once, load_registers


def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.logging.level)
    registers = load_registers(config.modbus.register_map, config.modbus.default_device_id)
    database = open_database(config.database.path)
    client = None
    previous = database.load_previous_cumulatives()

    try:
        with requests.Session() as session:
            LOGGER.info("Waiting for next interval before starting collectors")
            sleep_until_next_interval(config.scheduler.interval_seconds)
            last_forecast_scan: float | None = None
            while True:
                if client is None:
                    try:
                        candidate = ModbusTcpClient(config.modbus.host, port=config.modbus.port, timeout=config.modbus.timeout_seconds)
                        if candidate.connect():
                            client = candidate
                        else:
                            candidate.close()
                            LOGGER.info("Modbus connection failed; retrying next cycle")
                    except Exception:
                        LOGGER.info("Modbus connection failed; retrying next cycle")
                if client is not None and not collect_modbus_once(client, database, registers, previous, config.timezone):
                    client.close()
                    client = None

                now = time.monotonic()

                if last_forecast_scan is None or now - last_forecast_scan >= config.forecast.interval_seconds:
                    collect_forecast_once(session, database, config.forecast.arrays, config.forecast.endpoint, config.forecast.timeout_seconds, config.timezone)
                    last_forecast_scan = now
                sleep_until_next_interval(config.scheduler.interval_seconds)
    finally:
        if client is not None:
            client.close()
        database.close()
        LOGGER.info("collectors stopped")


if __name__ == "__main__":
    main()
