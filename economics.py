"""Tariff lookup and stored per-sample economics calculations."""

from __future__ import annotations

from datetime import datetime

from config import TariffRule, TariffsConfig, tariff_rule_matches


def tariff_rate(rules: tuple[TariffRule, ...], timestamp: datetime) -> float:
    """Return the rate for a local timestamp.

    Configuration validation guarantees that precisely one rule matches.
    """
    minute_of_day = timestamp.hour * 60 + timestamp.minute
    for rule in rules:
        if tariff_rule_matches(rule, timestamp.weekday(), minute_of_day):
            return rule.rate
    raise ValueError(f"no tariff rule matches {timestamp:%a %H:%M}")


def economic_period_values(
    grid_import_kwh: float | None,
    grid_export_kwh: float | None,
    load_kwh: float | None,
    tariffs: TariffsConfig,
    collected_at_local: datetime,
) -> dict[str, float | None]:
    """Calculate amounts for the energy accumulated since the prior sample."""
    import_rate = tariff_rate(tariffs.import_, collected_at_local)
    export_rate = tariff_rate(tariffs.export, collected_at_local)
    import_cost = (
        grid_import_kwh * import_rate if grid_import_kwh is not None else None
    )
    export_revenue = (
        grid_export_kwh * export_rate if grid_export_kwh is not None else None
    )
    return {
        "import_rate": import_rate,
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
