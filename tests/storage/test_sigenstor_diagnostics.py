"""Optional hardware diagnostics must preserve healthy telemetry and zero states."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from solar_model.config import load_config
from solar_model.integrations.sigenstor.client import read_registers
from solar_model.integrations.sigenstor.registers import DEVICE_REGISTER_MAP_PATH, REGISTER_MAP_PATH, load_registers
from solar_model.services.collection import collect_once
from solar_model.storage.migrations import open_database
from solar_model.storage.queries import dashboard as data
from solar_model.storage.schema import SigenStorDevice, SigenStorHourlySample


def response(words):
    return SimpleNamespace(isError=lambda: False, registers=words)


def test_optional_failures_preserve_required_sample_and_other_metadata(tmp_path):
    database = open_database(tmp_path / "diagnostics.db")
    electrical = load_registers(REGISTER_MAP_PATH, 247, metrics={"plant_grid_power_kw", "inverter_internal_temperature", "inverter_battery_avg_cell_temperature"})
    metadata = load_registers(DEVICE_REGISTER_MAP_PATH, 247, metrics={"Inverter firmware", "EMS work mode", "Plant alarm 1", "Plant alarm 6"})
    client = Mock()

    def read(address, count, device_id):
        if address == 30005:
            return response([0, 2000])
        if address == 31003:
            return response([])  # Short optional temperature response.
        if address == 30603:
            raise OSError("temporary diagnostic failure")
        if address == 30280:
            return SimpleNamespace(isError=lambda: True)
        if address == 30525:
            payload = b"V100R001C00SPC117".ljust(30, b"\x00")
            return response([int.from_bytes(payload[i:i + 2], "big") for i in range(0, 30, 2)])
        return response([0])

    client.read_input_registers.side_effect = read
    try:
        assert collect_once(client, database, electrical, {}, "UTC", load_config().tariffs, metadata)
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT plant_grid_power_kw, inverter_internal_temperature_c, inverter_battery_avg_cell_temperature_c FROM sigenstor").one() == (20000, None, None)
        displayed = {row["variable"]: row["value"] for row in data.load_device_information(str(tmp_path / "diagnostics.db"))}
        assert displayed == {"Inverter firmware": "V100R001C00SPC117", "EMS work mode": "0 (Maximum self-consumption)", "Plant alarm 1": "0x0000 (none)"}
        # Optional reads must not widen any required span or request an unsupported gap.
        assert {(call.kwargs["address"], call.kwargs["count"]) for call in client.read_input_registers.call_args_list} == {(30005, 2), (31003, 1), (30603, 1), (30525, 15), (30003, 1), (30027, 1), (30280, 1)}
        client.read_input_registers.side_effect = lambda **kwargs: SimpleNamespace(isError=lambda: True)
        assert not collect_once(client, database, electrical, {}, "UTC", load_config().tariffs, metadata)
    finally:
        database.close()


def test_alarm_clear_and_zero_states_are_saved_without_repeated_history(tmp_path):
    path = tmp_path / "states.db"
    database = open_database(path)
    blocks = load_registers(DEVICE_REGISTER_MAP_PATH, 247, metrics={"EMS work mode", "Inverter output type", "Inverter running state", "Plant alarm 1"})
    names = {register.metric: register.variable_name for block in blocks for register in block.registers}
    client = Mock()
    try:
        for minute, word in enumerate([1, 0, 0]):
            client.read_input_registers.return_value = response([word])
            values = read_registers(client, blocks)
            stamp = f"2026-11-15T10:0{minute}:00+00:00"
            assert database.save_device_info(values, (stamp, stamp), names) == (0 if minute == 2 else 4)
        with database.sessions() as session:
            history = session.scalars(select(SigenStorDevice).where(SigenStorDevice.variable == "Plant alarm 1").order_by(SigenStorDevice.id)).all()
            assert [row.value for row in history] == ["1", "0"]
        displayed = {row["variable"]: row["value"] for row in data.load_device_information(str(path))}
        assert displayed["Inverter output type"] == "0 (L/N)"
        assert displayed["Inverter running state"] == "0 (Standby)"
        assert displayed["Plant alarm 1"] == "0x0000 (none)"
        # Legacy empty/zero identity readings retain their rejection behavior.
        assert database.save_device_info({"Inverter model": (0., "", False)}, (stamp, stamp)) == 0
    finally:
        database.close()


def test_temperature_decode_rollup_and_export_preserve_missing_and_negative_values(tmp_path):
    path = tmp_path / "temperatures.db"
    database = open_database(path)
    config = load_config()
    blocks = load_registers(REGISTER_MAP_PATH, 247, metrics={"inverter_internal_temperature", "inverter_battery_avg_cell_temperature"})
    client = Mock()
    previous = {}
    try:
        for minute, raw in enumerate([0xFFCE, None, 0, 350]):  # -5, missing, 0 and 35 C.
            values = {}
            if raw is not None:
                client.read_input_registers.return_value = response([raw])
                values = read_registers(client, blocks)
            stamp = f"2026-11-15T10:0{minute}:00+00:00"
            database.save_modbus_sample(values, previous, (stamp, stamp), config.tariffs)
        with database.engine.connect() as connection:
            assert [row[0] for row in connection.exec_driver_sql("SELECT inverter_internal_temperature_c FROM sigenstor ORDER BY collected_at_utc")] == [-50000, None, 0, 350000]
        def check_rollup():
            with database.sessions() as session:
                rows = session.scalars(select(SigenStorHourlySample)).all()
                assert sum(row.inverter_internal_temperature_c for row in rows) == 30
                assert sum(row.inverter_internal_temperature_c_sample_count for row in rows) == 3
                assert sum(row.inverter_battery_avg_cell_temperature_c_sample_count for row in rows) == 3
        check_rollup()
        database.rebuild_hourly_rollup(date(2026, 11, 15), date(2026, 11, 15), ZoneInfo("UTC"))
        check_rollup()
        bounds = data.analysis_date_bounds(date(2026, 11, 15), date(2026, 11, 15), ZoneInfo("UTC"))
        exported = data.analysis_export_dataframe(str(path), bounds, "hour", ZoneInfo("UTC"), include_forecast=False)
        assert exported["Inverter Internal Temperature (°C)"].iloc[0] == pytest.approx(10)
        assert exported["Battery Average Cell Temperature (°C)"].iloc[0] == pytest.approx(10)
    finally:
        database.close()


def test_existing_database_adds_nullable_temperature_columns_without_backfill(tmp_path):
    path = tmp_path / "old.db"
    database = open_database(path)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO sigenstor (collected_at_utc, plant_grid_power_kw) VALUES (0, 12345)")
        for table in ["sigenstor", "sigenstor_hourly"]:
            for field in ["inverter_internal_temperature_c", "inverter_battery_avg_cell_temperature_c"]:
                connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {field}")
                if table == "sigenstor_hourly":
                    connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {field}_sample_count")
    database.close()
    for _ in range(2):
        migrated = open_database(path)
        try:
            with migrated.engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT plant_grid_power_kw, inverter_internal_temperature_c, inverter_battery_avg_cell_temperature_c FROM sigenstor").one() == (12345, None, None)
                assert connection.exec_driver_sql("SELECT COUNT(*) FROM sigenstor_hourly").scalar_one() == 0
        finally:
            migrated.close()
