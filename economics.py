"""Tariff lookup and stored per-sample economics calculations."""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from config import Config, TariffRule, TariffsConfig

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def tariff_time_minutes(value: str, *, allow_end_of_day: bool = False) -> int:
    if allow_end_of_day and value == "24:00":
        return 1440
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        maximum = "24:00" if allow_end_of_day else "23:59"
        raise ValueError(f"tariff time must use HH:MM from 00:00 to {maximum}: {value!r}")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def tariff_rule_matches(rule: TariffRule, weekday_index: int, minute_of_day: int) -> bool:
    start_minute, end_minute = tariff_time_minutes(rule.start_time), tariff_time_minutes(rule.end_time, allow_end_of_day=True)
    weekday = WEEKDAYS[weekday_index]
    if start_minute == end_minute:
        return weekday in rule.days
    if start_minute < end_minute:
        return weekday in rule.days and start_minute <= minute_of_day < end_minute
    return (weekday in rule.days and minute_of_day >= start_minute) or (WEEKDAYS[(weekday_index - 1) % 7] in rule.days and minute_of_day < end_minute)


def tariff_rules_for_timestamp(tariffs: TariffsConfig, timestamp: datetime) -> tuple[tuple[TariffRule, ...], tuple[TariffRule, ...]]:
    local_date = timestamp.date().isoformat()
    for period in reversed(tariffs.periods):
        if period.effective_from <= local_date:
            return period.import_, period.export
    raise ValueError(f"no tariff period covers {local_date}")


def tariff_rule(rules: tuple[TariffRule, ...], timestamp: datetime) -> TariffRule:
    """Return the matching tariff rule for a local timestamp.

    Configuration validation guarantees that precisely one rule matches.
    """
    minute_of_day = timestamp.hour * 60 + timestamp.minute
    for rule in rules:
        if tariff_rule_matches(rule, timestamp.weekday(), minute_of_day):
            return rule
    raise ValueError(f"no tariff rule matches {timestamp:%a %H:%M}")


def economic_period_values(
    grid_import_kwh: float | None,
    grid_export_kwh: float | None,
    load_kwh: float | None,
    tariffs: TariffsConfig,
    collected_at_local: datetime,
) -> dict[str, float | str | None]:
    """Calculate amounts for the energy accumulated since the prior sample."""
    import_rules, export_rules = tariff_rules_for_timestamp(tariffs, collected_at_local)
    import_rule = tariff_rule(import_rules, collected_at_local)
    import_rate = import_rule.rate
    export_rate = tariff_rule(export_rules, collected_at_local).rate
    import_cost = (
        grid_import_kwh * import_rate if grid_import_kwh is not None else None
    )
    export_revenue = (
        grid_export_kwh * export_rate if grid_export_kwh is not None else None
    )
    return {
        "import_rate": import_rate,
        "import_tariff_band": import_rule.name,
        "export_rate": export_rate,
        "grid_import_cost_period": import_cost,
        "grid_export_revenue_period": export_revenue,
        "net_cost_period": (
            import_cost - export_revenue
            if import_cost is not None and export_revenue is not None
            else None
        ),
        "no_solar_battery_import_cost_period": (
            load_kwh * import_rate if load_kwh is not None else None
        ),
    }
