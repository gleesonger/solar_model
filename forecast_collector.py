from __future__ import annotations

import requests

from common import LOGGER, heavy_work, timestamps
from config import SolarArrayConfig
from forecast.solar_adjustment import adjust_pending_forecasts


def fetch_array(session: requests.Session, array: SolarArrayConfig, endpoint: str, timeout_seconds: float) -> dict:
    url = (
        f"{endpoint}/{array.latitude}/{array.longitude}/"
        f"{array.declination}/{array.azimuth}/{array.peak_kw}"
    )
    response = session.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if payload.get("message", {}).get("type") not in (None, "success"):
        raise RuntimeError(payload["message"])
    return payload


@heavy_work("solar forecast collection update")
def collect_once(session: requests.Session, database, arrays: tuple[SolarArrayConfig, ...], endpoint: str, timeout_seconds: float, timezone_name: str) -> None:
    collected = timestamps(timezone_name=timezone_name)
    for array in arrays:
        try:
            count = database.save_forecast(array, fetch_array(session, array, endpoint, timeout_seconds), collected)
            LOGGER.info("saved %s forecast rows for %s", count, array.name)
        except Exception:
            LOGGER.exception("forecast request failed for %s; interval skipped", array.name)
    # Adjust only after every array has contributed to the same wide snapshot;
    # the model learns and corrects the combined site PV output.
    try:
        adjusted = adjust_pending_forecasts(database, collected[0], timezone_name)
        LOGGER.info("saved %s adjusted solar forecast rows", adjusted)
    except Exception:
        # Raw API data is the important first-class record. A failed model fit
        # leaves these rows pending for the safe, resumable backfill command.
        LOGGER.exception("solar forecast adjustment failed; raw rows remain pending")
