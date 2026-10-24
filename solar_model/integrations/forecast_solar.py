from __future__ import annotations
import requests
from solar_model.config import SolarArrayConfig

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
