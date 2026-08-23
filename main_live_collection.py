from __future__ import annotations

import logging

from pymodbus.client import ModbusTcpClient

from common import LOGGER, sleep_until_next_interval
from config import load_config
from database import open_database
from live_collector import LIVE_METRICS, collect_once
from modbus_collector import load_registers


def main() -> None:
    config = load_config()
    scheduler = config.live.scheduler
    modbus = config.actuals.modbus
    logging.basicConfig(level=config.logging.level)
    registers = load_registers(
        modbus.register_map,
        modbus.default_device_id,
        metrics=LIVE_METRICS,
    )
    database = open_database(config.database.path)
    client: ModbusTcpClient | None = None

    try:
        LOGGER.info("Waiting for next live interval before starting collector")
        sleep_until_next_interval(scheduler.interval_seconds)
        while True:
            if client is None:
                try:
                    candidate = ModbusTcpClient(
                        modbus.host,
                        port=modbus.port,
                        timeout=modbus.timeout_seconds,
                    )
                    if candidate.connect():
                        client = candidate
                    else:
                        candidate.close()
                        LOGGER.info("Live Modbus connection failed; retrying next cycle")
                except Exception:
                    LOGGER.info("Live Modbus connection failed; retrying next cycle")
            if client is not None and not collect_once(
                client, database, registers, config.timezone
            ):
                client.close()
                client = None
            sleep_until_next_interval(scheduler.interval_seconds)
    finally:
        if client is not None:
            client.close()
        database.close()
        LOGGER.info("live collector stopped")


if __name__ == "__main__":
    main()
