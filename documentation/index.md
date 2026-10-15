# Project guide

Solar_model collects Sigenergy solar/battery telemetry and Forecast.Solar predictions, stores their history in SQLite, and presents a NiceGUI dashboard. Separate scripts evaluate forecasts, compare tariffs, and optimise hypothetical battery-energy plans.

## Reading order

1. [Architecture](overview/architecture.md): processes and data flow.
2. [Code map](overview/code-map.md): responsibilities and where to make changes.
3. [Data model](overview/data-model.md): storage, units, timestamps, and invariants.
4. Read the relevant subsystem guide below before editing it.

For function hierarchies, query details, and data structures at each pipeline stage, continue to the [implementation reference](implementation/index.md). It traces ingestion through processing, persistence, retrieval, and presentation/planning with field tables and synthetic examples.

## Folder layout

```text
documentation/
  index.md          Start here: reading order and navigation
  overview/         Concise architecture and subsystem guides
  implementation/   Detailed call hierarchies and data pipelines
  operations/       Development, testing, and runtime procedures
  reference/        Exact schema fields and vendor protocol PDF
  reviews/          Review findings and unresolved questions
```

Keep concise explanations in `overview/`, detailed code/data contracts in `implementation/`, procedures in `operations/`, and field/vendor references in `reference/`. Record issues in `reviews/`; link to their context rather than repeating the full finding elsewhere. The repository-root `README.md` remains the practical project introduction.

## Guides

| Guide | Contents |
| --- | --- |
| [Configuration](overview/configuration.md) | YAML structure, validation, tariff rules |
| [Collection](overview/collection.md) | Scheduling, register decoding, persistence, failures |
| [Forecasting](overview/forecasting.md) | Operational correction, snapshot selection, research models |
| [Energy planning](overview/energy-planning.md) | Economics, optimisation, as-of workbooks, tariff experiments |
| [Dashboard](overview/dashboard.md) | Tabs, caches, live readings, queries, exports, reconnects |
| [Styling](overview/styling.md) | CSS, colours, chart conventions, responsive layouts |
| [Development and testing](operations/development-and-testing.md) | Setup, checks, coverage, verification limits |
| [Operations](operations/runbook.md) | Launch, Docker, restart, maintenance, troubleshooting |
| [Review findings](reviews/findings.md) | Confirmed defects and open questions |
| [Documentation development trial](reviews/documentation-development-trial.md) | Fresh-agent proposals tested against source, and resulting documentation gaps |
| [Download performance](reviews/download-performance.md) | Profiling findings, SQL/Excel optimisations, measured timings, and regression coverage |
| [Schema reference](reference/schema.md) | Every declared database column, key, type, and index |

## Essential distinctions

- Persisted collection and live dashboard polling use separate Modbus connections and schedules.
- Raw provider forecasts, adjusted forecasts, and observed solar energy have different meanings.
- Battery energy flow, available discharge energy, and state of charge are separate quantities.
- Planning produces reports; it is not connected to device control.
- Research/backfill scripts can use different models from the production collector.

## Review baseline and maintenance

- Reviewed on **2026-10-14**, using source, configuration, tests, and the conversation visible to this session.
- Initial Git baseline: `c6b1e81776b58ecf98502de21c7637fdd45fb6d1`, plus existing dashboard/warning changes. During review those changes were committed externally as `6452e13` (`consolidate warnings`); final source check used that revision plus existing README edits and untracked load evaluation. This documentation change does not modify application code.
- Chat decisions included: concise guides with distinct points; a dedicated styling guide; documentation maintained with future changes through root [AGENTS.md](../AGENTS.md). Other historical chats were not available for review.
- Source links use file paths and symbol names rather than fragile line numbers. Each guide describes the reviewed implementation, not a promise of intended behavior.
- The [Modbus protocol PDF](reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf) is retained under `reference/`; this review did not independently validate hardware behavior against the full PDF.
