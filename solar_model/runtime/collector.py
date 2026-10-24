from __future__ import annotations

import requests
from pymodbus.client import ModbusTcpClient

from solar_model.logging_setup import LOGGER, configure_logging
from solar_model.time_utils import sleep_until_next_interval
from solar_model.config import load_config
from solar_model.storage.migrations import open_database
from solar_model.services.forecasting import collect_once as collect_forecast_once
from solar_model.integrations.sigenstor.registers import (
    DEVICE_REGISTER_MAP_PATH,
    REGISTER_MAP_PATH,
    load_registers,
)
from solar_model.services.collection import collect_once as collect_modbus_once


def main() -> None:
    config = load_config()
    data_retrival_schedule = config.actuals.data_retrival_schedule
    modbus = config.actuals.modbus
    configure_logging(config.logging.level)
    registers = load_registers(REGISTER_MAP_PATH, modbus.default_device_id)
    device_registers = load_registers(DEVICE_REGISTER_MAP_PATH, modbus.default_device_id)
    database = open_database(config.database.path)
    client = None
    previous = database.load_previous_cumulatives()

    try:
        with requests.Session() as session:
            LOGGER.info("Waiting for next interval before starting collectors")
            sleep_until_next_interval(data_retrival_schedule.full_updated_interval_seconds)
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
                if client is not None and not collect_modbus_once(
                    client,
                    database,
                    registers,
                    previous,
                    config.timezone,
                    config.tariffs,
                    device_registers,
                ):
                    client.close()
                    client = None

                collect_forecast_once(
                    session, database, config.forecast.arrays, config.forecast.endpoint,
                    config.forecast.timeout_seconds, config.timezone, config.forecast.interval_seconds,
                    config.forecast.model_calibration_parameters,
                )
                sleep_until_next_interval(data_retrival_schedule.full_updated_interval_seconds)
    finally:
        if client is not None:
            client.close()
        database.close()
        LOGGER.info("collectors stopped")


if __name__ == "__main__":
    main()
