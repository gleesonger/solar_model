"""Apply database schema migrations and historical backfills."""

import argparse
from pathlib import Path

from solar_model.storage.migrations import DATABASE_VERSION, open_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Existing database to migrate")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"Database does not exist: {args.database}")
    database = open_database(args.database)
    database.close()
    print(f"Database migrated to {DATABASE_VERSION}: {args.database}")


if __name__ == "__main__":
    main()
