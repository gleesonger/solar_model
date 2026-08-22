from __future__ import annotations

import requests
from uuid import uuid4

from common import LOGGER, timestamps
from config import SolarArrayConfig


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


def collect_once(session: requests.Session, database, arrays: tuple[SolarArrayConfig, ...], endpoint: str, timeout_seconds: float, timezone_name: str) -> None:
    collected = timestamps(timezone_name=timezone_name)
    collection_guid = str(uuid4())
    for array in arrays:
        try:
            count = database.save_forecast(array, fetch_array(session, array, endpoint, timeout_seconds), collected, collection_guid)
            LOGGER.info("saved %s forecast rows for %s", count, array.name)
        except Exception:
            LOGGER.exception("forecast request failed for %s; interval skipped", array.name)
