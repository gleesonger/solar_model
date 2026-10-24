"""External forecast cadence survives restarts, partial responses and failures."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from solar_model.config import ForecastModelConfig, ForecastModelsConfig, SolarArrayConfig
from solar_model.storage.schema import DatabaseMetadata, ForecastSolarSample
from solar_model.storage.migrations import open_database
import solar_model.services.forecasting as collector


NOW = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
INTERVAL = 3600
ARRAYS = (SolarArrayConfig(1, "Roof", 53., -6., 30., 0., 3.),
          SolarArrayConfig(2, "Garden", 53., -6., 30., 90., 2.))


@pytest.fixture
def database(tmp_path):
    db = open_database(tmp_path / "forecast.db")
    yield db
    db.close()


@pytest.fixture
def requests(monkeypatch):
    fetch = Mock(return_value={"result": {"watts": {"2026-10-15T12:00:00Z": 1000.}}})
    adjust = Mock(return_value=1)
    monkeypatch.setattr(collector, "fetch_array", fetch)
    monkeypatch.setattr(collector, "refresh_adjusted_forecast", adjust)
    return fetch, adjust


def run(database, monkeypatch, now=NOW):
    monkeypatch.setattr(collector, "utc_now", lambda: now)
    collector.collect_once(Mock(), database, ARRAYS, "https://example.test/estimate", 10., "Europe/Dublin", INTERVAL)


def test_collection_passes_yaml_model_settings_to_adjustment(database, requests, monkeypatch):
    models = ForecastModelsConfig(solar_intraday=ForecastModelConfig(2., 0.5, 0.5))
    load_adjustment = Mock(return_value=24)
    monkeypatch.setattr(collector, "refresh_daily_load_forecast", load_adjustment)
    monkeypatch.setattr(collector, "utc_now", lambda: NOW)
    collector.collect_once(Mock(), database, ARRAYS, "https://example.test/estimate", 10., "Europe/Dublin", INTERVAL, models)
    assert requests[1].call_args.kwargs["models"] is models
    assert load_adjustment.call_args.kwargs["models"] is models


def test_first_scan_fetches_arrays_then_restart_skips_until_due(database, requests, monkeypatch, caplog):
    fetch, adjust = requests
    run(database, monkeypatch)
    assert fetch.call_count == 2
    adjust.assert_called_once()
    with database.sessions() as session:
        assert session.scalar(select(DatabaseMetadata.value).where(DatabaseMetadata.key == "forecast_last_attempt_utc")) == NOW.isoformat()
    restarted = open_database(database.engine.url.database)
    try:
        with caplog.at_level("INFO", logger="solar_model"):
            run(restarted, monkeypatch, NOW + timedelta(minutes=15))
        assert fetch.call_count == 2
        assert adjust.call_count == 1
        assert "Skipping external solar forecast request" in caplog.text
        assert "last collection attempt 2026-10-15T11:00:00+01:00" in caplog.text
        assert "next due 2026-10-15T12:00:00+01:00" in caplog.text
        run(restarted, monkeypatch, NOW + timedelta(seconds=INTERVAL))
        assert fetch.call_count == 4
        assert adjust.call_count == 2
    finally:
        restarted.close()


@pytest.mark.parametrize("age,due", [(60, False), (3599, False), (3600, True), (3601, True), (-120, False)])
def test_existing_forecast_history_is_respected_without_attempt_marker(database, requests, monkeypatch, age, due):
    with database.sessions.begin() as session:
        session.add(ForecastSolarSample(collected_at_utc=(NOW - timedelta(seconds=age)).isoformat(),
                                       forecast_time="2026-10-15T12:00:00Z", panel_1_raw_watts=1000.))
    run(database, monkeypatch)
    fetch, adjust = requests
    assert fetch.call_count == (2 if due else 0)
    assert adjust.call_count == (1 if due else 0)


def test_failed_attempt_also_survives_restart_and_keeps_schedule(database, requests, monkeypatch):
    fetch, _ = requests
    fetch.side_effect = RuntimeError("provider unavailable")
    run(database, monkeypatch)
    assert fetch.call_count == 2
    restarted = open_database(database.engine.url.database)
    try:
        run(restarted, monkeypatch, NOW + timedelta(minutes=1))
        assert fetch.call_count == 2
        run(restarted, monkeypatch, NOW + timedelta(hours=1))
        assert fetch.call_count == 4
    finally:
        restarted.close()


def test_same_due_scan_cannot_be_claimed_twice(database):
    other = open_database(database.engine.url.database)
    try:
        assert database.claim_forecast_collection(NOW, INTERVAL) == (True, None)
        assert other.claim_forecast_collection(NOW, INTERVAL) == (False, NOW)
    finally:
        other.close()


def test_schedule_database_failure_skips_http_and_allows_next_cycle(database, requests, monkeypatch):
    fetch, adjust = requests
    with monkeypatch.context() as patch:
        patch.setattr(database, "claim_forecast_collection", Mock(side_effect=SQLAlchemyError("database locked")))
        run(database, patch)
    fetch.assert_not_called()
    adjust.assert_not_called()
    run(database, monkeypatch)
    assert fetch.call_count == 2


def test_schedule_uses_elapsed_utc_time_across_autumn_fold(database):
    first = datetime.fromisoformat("2026-10-25T01:30:00+01:00")
    repeated = datetime.fromisoformat("2026-10-25T01:30:00+00:00")
    assert database.claim_forecast_collection(first, INTERVAL)[0]
    assert database.claim_forecast_collection(repeated, INTERVAL)[0]
