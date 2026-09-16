from datetime import date
from pathlib import Path

from dashboard.control import one_year_before, recalculation_command, restart_application


def test_recalculation_command_passes_an_inclusive_date_range() -> None:
    command = recalculation_command(
        date(2026, 9, 1),
        date(2026, 9, 15),
        Path("C:/config/config.yaml"),
    )

    assert command[-4:] == ["--from", "2026-09-01", "--to", "2026-09-15"]
    assert "--config" in command


def test_one_year_before_handles_leap_day() -> None:
    assert one_year_before(date(2024, 2, 29)) == date(2023, 2, 28)


def test_restart_application_requests_supervisor_restart(tmp_path, monkeypatch) -> None:
    restart_file = tmp_path / "restart-request"
    monkeypatch.setenv("SOLAR_MODEL_RESTART_FILE", str(restart_file))

    restart_application()

    assert restart_file.exists()
