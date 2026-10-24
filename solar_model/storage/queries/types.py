from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

SUMMARY_LATEST_KEYS = {
    "Solar": "solar",
    "Battery": "battery",
    "Load": "load",
    "Import": "grid_import",
    "Export": "grid_export",
}


@dataclass(frozen=True)
class TelemetryData:
    power_15m: pd.DataFrame
    battery_15m: pd.DataFrame
    latest: dict[str, float | None]
    latest_collected_at_utc: str | None
    full_updated_last_local: str | None


@dataclass(frozen=True)
class AnalysisRangeData:
    energy: pd.DataFrame
    power: pd.DataFrame
    battery: pd.DataFrame

@dataclass(frozen=True)
class LivePowerData:
    collected_at_utc: str | None
    collected_at_local: str | None
    values: dict[str, float | None]
