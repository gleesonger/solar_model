from dashboard.data import load_grid_energy_day_start
from database import SigenStorModbusSample, open_database


def test_grid_energy_day_start_uses_configured_timezone_not_host_timezone(tmp_path) -> None:
    path = tmp_path / "solar.db"
    database = open_database(path)
    try:
        with database.sessions.begin() as session:
            session.add_all([
                SigenStorModbusSample(
                    collected_at_utc="2026-09-20T22:30:00+00:00",
                    plant_grid_import_total_kwh=100.0,
                    plant_grid_export_total_kwh=200.0,
                ),
                SigenStorModbusSample(
                    collected_at_utc="2026-09-20T23:30:00+00:00",
                    plant_grid_import_total_kwh=101.0,
                    plant_grid_export_total_kwh=202.0,
                ),
            ])
    finally:
        database.engine.dispose()

    assert load_grid_energy_day_start(str(path), "2026-09-21", "Europe/Dublin") == {
        "today_grid_import": 101.0,
        "today_grid_export": 202.0,
    }
