"""One-off migration for pre-tariff solar-model databases.

The script drops the obsolete ``sigenstor_live`` table and backfills economics
from the currently configured tariff rules. It intentionally recalculates all
sample economics, so run it only when the current config represents the rates
you want applied to historical samples.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
from numbers import Real
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, inspect, text

from config import TariffRule, load_config, tariff_rule_matches
from database import open_database


SAMPLE_COLUMNS = (
    "plant_grid_import_total_kwh_period",
    "plant_grid_export_total_kwh_period",
    "plant_load_total_kwh_period",
)
ECONOMICS_COLUMNS = (
    "import_rate",
    "export_rate",
    "grid_import_cost_period",
    "grid_export_revenue_period",
    "net_cost_period",
    "no_solar_battery_import_cost_period",
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        help="SQLite database to migrate; defaults to database.path in the config",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Configuration file that provides the current tariffs",
    )
    return parser.parse_args()


def tariff_lookup_table(
    rules: Sequence[TariffRule],
    rate_column: str,
) -> pd.DataFrame:
    """Create the minute-resolution tariff table used for the pandas join."""
    rows: list[dict[str, int | float]] = []
    for weekday_index in range(7):
        for minute_of_day in range(24 * 60):
            matching = [
                rule
                for rule in rules
                if tariff_rule_matches(rule, weekday_index, minute_of_day)
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"expected one {rate_column} tariff for weekday {weekday_index}, "
                    f"minute {minute_of_day}; found {len(matching)}"
                )
            rows.append({
                "weekday_index": weekday_index,
                "minute_of_day": minute_of_day,
                rate_column: matching[0].rate,
            })
    return pd.DataFrame(rows)


def sample_time_parts(value: object) -> tuple[int, int]:
    if not isinstance(value, (datetime, pd.Timestamp)):
        raise ValueError(f"invalid collected_at_local timestamp: {value!r}")
    return value.weekday(), value.hour * 60 + value.minute


def load_samples(database_path: Path) -> pd.DataFrame:
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("sigenstor_samples")}
        missing_columns = set(SAMPLE_COLUMNS) - columns
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"sigenstor_samples is missing required columns: {missing}")
        with engine.connect() as connection:
            return pd.read_sql_query(
                text(
                    "SELECT id, collected_at_local, "
                    f"{', '.join(SAMPLE_COLUMNS)} FROM sigenstor_samples"
                ),
                connection,
            )
    finally:
        engine.dispose()


def calculate_economics(
    samples: pd.DataFrame,
    import_rules: Sequence[TariffRule],
    export_rules: Sequence[TariffRule],
) -> pd.DataFrame:
    if len(samples.index) == 0:
        return samples.reindex(columns=[*samples.columns, *ECONOMICS_COLUMNS])

    samples = samples.copy()
    samples["collected_at_local"] = pd.to_datetime(
        samples["collected_at_local"],
        format="mixed",
        errors="coerce",
    )
    if bool(samples["collected_at_local"].isna().any()):
        invalid_count = int(samples["collected_at_local"].isna().sum())
        raise ValueError(f"{invalid_count} sample timestamps could not be parsed")

    time_parts = samples["collected_at_local"].map(sample_time_parts)
    samples["weekday_index"] = time_parts.map(lambda parts: parts[0])
    samples["minute_of_day"] = time_parts.map(lambda parts: parts[1])
    samples = samples.merge(
        tariff_lookup_table(import_rules, "import_rate"),
        on=["weekday_index", "minute_of_day"],
        how="left",
        validate="many_to_one",
    ).merge(
        tariff_lookup_table(export_rules, "export_rate"),
        on=["weekday_index", "minute_of_day"],
        how="left",
        validate="many_to_one",
    )
    if (
        bool(samples["import_rate"].isna().any())
        or bool(samples["export_rate"].isna().any())
    ):
        raise ValueError("one or more samples could not be matched to a tariff")

    samples["grid_import_cost_period"] = (
        samples["plant_grid_import_total_kwh_period"] * samples["import_rate"]
    )
    samples["grid_export_revenue_period"] = (
        samples["plant_grid_export_total_kwh_period"] * samples["export_rate"]
    )
    samples["net_cost_period"] = (
        samples["grid_import_cost_period"] - samples["grid_export_revenue_period"]
    )
    samples["no_solar_battery_import_cost_period"] = (
        samples["plant_load_total_kwh_period"] * samples["import_rate"]
    )
    return samples


def optional_float(value: object) -> float | None:
    if value is None or bool(pd.isna(value)):
        return None
    if not isinstance(value, Real):
        raise ValueError(f"expected a numeric value, received {value!r}")
    return float(value)


def save_economics(database_path: Path, samples: pd.DataFrame) -> bool:
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    try:
        selected_columns = ["id", *ECONOMICS_COLUMNS]
        update_rows: list[dict[str, Any]] = []
        for values in samples[selected_columns].itertuples(index=False, name=None):
            row = dict(zip(selected_columns, values, strict=True))
            update_rows.append({
                key: optional_float(value) if key != "id" else int(value)
                for key, value in row.items()
            })
        with engine.begin() as connection:
            if update_rows:
                connection.execute(
                    text(
                        "UPDATE sigenstor_samples SET "
                        "import_rate = :import_rate, "
                        "export_rate = :export_rate, "
                        "grid_import_cost_period = :grid_import_cost_period, "
                        "grid_export_revenue_period = :grid_export_revenue_period, "
                        "net_cost_period = :net_cost_period, "
                        "no_solar_battery_import_cost_period = :no_solar_battery_import_cost_period "
                        "WHERE id = :id"
                    ),
                    update_rows,
                )
            table_names = inspect(connection).get_table_names()
            live_table_dropped = "sigenstor_live" in table_names
            if live_table_dropped:
                connection.execute(text("DROP TABLE sigenstor_live"))
        return live_table_dropped
    finally:
        engine.dispose()


def main() -> None:
    arguments = parse_arguments()
    config = load_config(arguments.config)
    database_path = (arguments.database or Path(config.database.path)).resolve()
    if not database_path.is_file():
        raise ValueError(f"database does not exist: {database_path}")

    database = open_database(database_path)
    database.close()
    samples = load_samples(database_path)
    economics = calculate_economics(
        samples,
        config.tariffs.import_,
        config.tariffs.export,
    )
    live_table_dropped = save_economics(database_path, economics)
    print(
        f"Migrated {len(economics)} samples in {database_path}; "
        f"sigenstor_live {'dropped' if live_table_dropped else 'was not present'}."
    )


if __name__ == "__main__":
    main()
