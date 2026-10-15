# Architecture

Detailed startup/call hierarchies and object ownership are in [runtime.md](../implementation/runtime.md); stage-by-stage data contracts start at the [implementation index](../implementation/index.md).

## Runtime ownership

- [docker_start.py](../../docker_start.py) supervises two Python children, initialises the database, and handles full-application restart requests.
- [main_data_collection.py](../../main_data_collection.py) owns persisted collection: Modbus on each full interval, Forecast.Solar when due.
- [main_dashboard.py](../../main_dashboard.py) serves NiceGUI. It owns one shared snapshot worker and one browser-activated live Modbus collector.
- [database.py](../../database.py) owns ORM schema, automatic migrations, collector persistence, derived PV measurements, and hourly rollups. Maintenance scripts also issue direct SQL updates.
- [energy_plan_simulation.py](../../energy_plan_simulation.py) is a standalone optimiser called by the as-of workbook script, not the running dashboard.

```mermaid
flowchart LR
    Supervisor[Managed launcher] --> Collector[Persisted collector]
    Supervisor --> Dashboard[NiceGUI server]
    Device[Sigenergy Modbus] --> Collector
    Provider[Forecast.Solar API] --> Collector
    Collector --> Model[Operational forecast correction]
    Collector --> DB[(SQLite)]
    Model --> DB
    DB --> Cache[Shared Recent and device snapshot]
    Cache --> Dashboard
    Device --> Live[In-memory live collector]
    Live --> Dashboard
    DB --> Analysis[Analysis SQL and exports]
    Analysis --> Dashboard
    DB --> Planning[As-of planning workbook]
    Dashboard --> Browser[Browser UI and freshness checks]
```

## Execution and concurrency

- Collector work is sequential: full electrical sample, optional device information, then due forecast collection/correction. Slow work delays the next cycle; missed slots are not queued.
- The supervisor opens/migrates the database before starting either child. Children also open it at startup; standalone entry points therefore initialise it themselves.
- Dashboard database work runs in NiceGUI `run.io_bound` workers. The shared snapshot swaps references under a lock and notifies browser subscribers.
- Each browser owns its tab state, UI elements, live-refresh timer, and warning heartbeat. Recent/System Info share server data; Analysis queries are selected per browser.
- Live polling runs in a daemon thread and pauses when no browser socket is connected. It retains the newest successful reading without persisting it.
- SQLite coordinates access between processes. No explicit WAL setup, custom busy timeout, or central write queue is configured in the source.

## Design boundaries

- Hardware fetching/decoding belongs in collectors; storage calculations belong in `SolarDatabase`; UI formatting and aggregation belong in `dashboard/`.
- Browser JavaScript evaluates connectivity/freshness and the live-update indicator; Python publishes timestamps and sends heartbeats.
- Dashboard Control actions trigger tariff recalculation and process restarts, not inverter charge/discharge commands.
- Offline experiments and generated workbooks are separate from the managed application.

See [code-map.md](code-map.md) for implementation locations and [review findings](../reviews/findings.md) for boundary inconsistencies.
