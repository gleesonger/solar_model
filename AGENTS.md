# Working on solar_model

- Start with the [documentation index](documentation/index.md), then read the guides for the subsystem you are changing.
- For every code change, review the relevant overview and [implementation documentation](documentation/implementation/index.md) and consider its documented behavior, data contracts, assumptions, and constraints when making the change.
- **Keep documentation up to date with future changes.** Update the relevant Markdown guides in the same change as code, configuration, schema, commands, or styling. Update this file when agent instructions change.
- If a proposed change creates or reveals a major conflict with the documentation, notify the user, explain the conflict, and ask for direction before proceeding with the conflicting change.
- Document verified behavior; keep defects and unresolved design questions in [review findings](documentation/reviews/findings.md). Remove or revise findings when resolved.
- Preserve unrelated working-tree changes. Consult the documentation baseline for reviewed revision and working-tree context; Git HEAD alone may not describe local experimental files.
- Production entry points: `docker_start.py`, `main_data_collection.py`, `main_dashboard.py`. Planning and research scripts are separate; the application does not send battery-control commands.
- Database measurements use integers scaled by 10,000; SQLAlchemy returns physical units. Raw SQL must convert explicitly. Most measurement timestamps are UTC epoch seconds; device-history timestamps are ISO strings.
- Preserve raw forecast history, snapshot/target-time distinctions, cumulative-counter reset handling, and configured-timezone date boundaries. See [data-model.md](documentation/overview/data-model.md).
- `open_database()` can migrate and backfill a database. Use temporary databases or copies for development; do not use the user's `solar.db` for destructive verification.
- Run commands from the repository root unless a guide specifies otherwise. Configuration and relative database paths resolve from the working directory.
- Standard check: `python -m pytest -q`. The current missing migration-module import blocks full collection; the documented temporary check is `python -m pytest -q --ignore=tests/test_database_0_1_0_migration.py`. Never report that subset as a passing full suite.
- For styling changes, follow [styling.md](documentation/overview/styling.md) and check desktop/mobile layouts, chart legends, and loading/stale states.
