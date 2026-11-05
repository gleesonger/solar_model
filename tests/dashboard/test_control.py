from datetime import date
from pathlib import Path

import pytest

from solar_model.dashboard.tabs.control import holiday_periods_from_rows, one_year_before, recalculation_command, restart_application


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


def test_holiday_row_uses_entered_date_for_one_day_period() -> None:
    assert holiday_periods_from_rows([{"start_date": "2026-12-24", "end_date": ""}]) == [("2026-12-24", "2026-12-24")]
    assert holiday_periods_from_rows([{"start_date": "", "end_date": "2026-12-25"}]) == [("2026-12-25", "2026-12-25")]
    assert holiday_periods_from_rows([{"start_date": "", "end_date": ""}]) == []


def test_holiday_row_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end date must not precede start date"):
        holiday_periods_from_rows([{"start_date": "2026-12-25", "end_date": "2026-12-24"}])


@pytest.mark.parametrize("invalid", ["20261224", "2026-02-30"])
def test_holiday_row_rejects_invalid_iso_date(invalid) -> None:
    with pytest.raises(ValueError):
        holiday_periods_from_rows([{"start_date": invalid, "end_date": ""}])


def test_restart_application_requests_supervisor_restart(tmp_path, monkeypatch) -> None:
    restart_file = tmp_path / "restart-request"
    monkeypatch.setenv("SOLAR_MODEL_RESTART_FILE", str(restart_file))

    restart_application()

    assert restart_file.exists()
