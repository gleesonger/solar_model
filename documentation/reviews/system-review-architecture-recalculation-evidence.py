"""Temporary-DB reproduction of recalculation's partial commit/error message."""
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy.exc import SQLAlchemyError
from solar_model.config import DatabaseConfig, TariffPeriod, TariffRule, TariffsConfig, load_config
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.migrations import open_database
import solar_model.tools.recalculate_economics as recalculate_economics

root = Path(tempfile.mkdtemp(prefix="solar-architecture-recalculation-"))
path = root / "fixture.db"
stamp = int(datetime.fromisoformat("2026-10-01T12:00:00+00:00").timestamp())
db = open_database(path)
with db.engine.begin() as connection:
    connection.exec_driver_sql(
        "INSERT INTO sigenstor(collected_at_utc,import_tariff_band,import_rate,export_rate,"
        "plant_grid_import_total_kwh_period,plant_grid_export_total_kwh_period,"
        "plant_load_total_kwh_period,grid_import_cost_period,grid_export_revenue_period,"
        "net_cost_period,no_solar_battery_import_cost_period) "
        "VALUES(?, 'old',1000,1000,10000,0,10000,1000,0,1000,1000)", (stamp,)
    )
db.backfill_hourly_rollup()
db.close()
days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
config = replace(load_config(), database=DatabaseConfig(str(path)), tariffs=TariffsConfig((
    TariffPeriod("2000-01-01", (TariffRule(days, "00:00", "24:00", .5, "new"),),
                 (TariffRule(days, "00:00", "24:00", .1),)),
)))
stderr = StringIO()
with patch.object(recalculate_economics, "load_config", return_value=config), patch.object(
    SolarDatabase, "rebuild_hourly_rollup", side_effect=SQLAlchemyError("injected rollup failure")
), redirect_stderr(stderr):
    try:
        recalculate_economics.main(["--config", str(root / "unused.yaml"), "--from", "2026-10-01", "--to", "2026-10-01"])
    except SystemExit as error:
        print("Exit:", error.code)
print("Message:", stderr.getvalue().strip())
db = open_database(path)
with db.engine.connect() as connection:
    for table in ("sigenstor", "sigenstor_hourly"):
        print(table, connection.exec_driver_sql("SELECT import_tariff_band, import_rate, grid_import_cost_period FROM " + table).all())
db.close()
print("Temporary evidence:", root)
