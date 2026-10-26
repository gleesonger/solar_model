"""Create an as-of energy plan workbook without using future database data."""
from __future__ import annotations
import argparse
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from solar_model.config import load_config
from solar_model.storage.connection import SolarDatabase, create_engine_for_database


from solar_model.tools.reports import group_plan, write_workbook
from solar_model.services.planning import plan_as_of

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--database", type=Path, default=Path("solar.db"))
    parser.add_argument("--as-of", required=True, help="planning cut-off as an ISO date-time; system timezone is assumed when omitted")
    parser.add_argument("--output-timestep-min", type=int, default=None, help="optional start-time grouping in minutes; default preserves mixed model intervals")
    parser.add_argument("--output", type=Path, default=Path("energy_plan.xlsx"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        as_of = as_of.astimezone()
    as_of = as_of.astimezone(ZoneInfo(config.timezone))
    LOGGER.info("Planning as of %s", as_of.isoformat())
    LOGGER.info("Loading database %s", args.database)
    database = SolarDatabase(create_engine_for_database(args.database))
    try:
        Path(args.output).unlink(missing_ok=True)
        
        LOGGER.info("Loading planning inputs and solving scenarios")
        plans = plan_as_of(database, config, as_of).results

        if args.output_timestep_min is not None:
            if args.output_timestep_min <= 0:
                raise ValueError("--output-timestep-min must be greater than zero")
            plans = {name: group_plan(plan, args.output_timestep_min) for name, plan in plans.items()}
            LOGGER.info("Grouped %d plans into %d-minute start-time buckets", len(plans), args.output_timestep_min)
        write_workbook(plans, args.output, as_of)
    finally:
        database.engine.dispose()
    LOGGER.info("Wrote %s", args.output)

if __name__ == "__main__":
    main()
