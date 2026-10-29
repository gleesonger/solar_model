# Project guide

Solar_model collects Sigenergy solar/battery telemetry and Forecast.Solar predictions, stores their history in SQLite, and presents a NiceGUI dashboard. Separate scripts evaluate forecasts, compare tariffs, and optimise hypothetical battery-energy plans.

## Using this documentation

This is a reference for people and AI assistants answering questions about the application. Preserve file paths when sharing or indexing the documentation so links and citations remain meaningful.

| Question | Primary source |
| --- | --- |
| What does the application do? | `overview/`: behavior, concepts and subsystem boundaries |
| How is it implemented? | `implementation/`: calls, calculations, queries and data contracts |
| What exactly does a field mean? | `reference/`: schema, units, signs and aggregation semantics |
| How do I run, test or maintain it? | `operations/`: commands, prerequisites and write effects |
| What is broken, undecided or unverified? | `reviews/findings.md` and `reviews/verification.md` |
| What did an investigation measure? | Dated performance/register investigations in `reviews/` |

Use subsystem guides and exact references for current behavior. Findings qualify that behavior with known limitations; proposals and unresolved decisions are not implemented features. Historical measurements describe their stated dataset, date and environment, not current performance or universal device behavior. Synthetic examples illustrate contracts and are not readings from the user's installation.

For answers, name the relevant file and section, retain units and timestamp/vintage distinctions, and distinguish documented facts from inference. If documents disagree, report the discrepancy and consult the relevant source when available; do not silently combine incompatible descriptions. If the supplied material does not establish an answer, state what is unknown. A documentation edit date is not evidence that the code or hardware was reverified that day.

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
| [Plan model](overview/plan-model.md) | Current next-action/report model: forecasts, joint scenarios, scoring, physical constraints and remaining limitations |
| [Dashboard](overview/dashboard.md) | Tabs, caches, live readings, queries, exports, reconnects |
| [Styling](overview/styling.md) | CSS, colours, chart conventions, responsive layouts |
| [Development and testing](operations/development-and-testing.md) | Setup, checks, coverage, verification limits |
| [Isolated development](operations/isolated-development.md) | Temporary-database tests and synthetic full-dashboard walkthrough |
| [Adding a metric](implementation/adding-a-metric.md) | Worked ingestion → storage → statistics → query → chart/export change recipe |
| [Operations](operations/runbook.md) | Launch, Docker, restart, maintenance, troubleshooting |
| [Review findings](reviews/findings.md) | Unresolved defects, policy decisions and verification gaps |
| [Verification scope](reviews/verification.md) | Regression coverage and remaining integration/verification limits |
| [Performance conclusions](reviews/performance.md) | Key download, summary, forecasting and planning measurements |
| [Sigenergy register review](reviews/sigenergy-home-assistant-register-review.md) | Load boundaries, electrical residuals and diagnostic priorities |
| [Schema reference](reference/schema.md) | Every declared database column, key, type, and index |
| [Metric semantics](reference/metric-semantics.md) | Units, signs, missing values, time attribution and chart/download aggregation differences |

## Essential distinctions

- Persisted collection and live dashboard polling use separate Modbus connections and schedules.
- Raw provider forecasts, adjusted forecasts, and observed solar energy have different meanings.
- Battery energy flow, available discharge energy, and state of charge are separate quantities.
- Planning produces reports; it is not connected to device control.
- Research/backfill scripts can use different models from the production collector.

## Maintenance and verification

- Maintain overview, implementation and reference contracts with their code changes, as required by [AGENTS.md](../AGENTS.md). Keep unresolved defects and decisions in [findings](reviews/findings.md), removing them when resolved.
- Use source paths and symbol names for implementation links. The workspace can include uncommitted changes; Git HEAD alone does not establish the implementation being described.
- Run the checks in [development and testing](operations/development-and-testing.md) for the revision being assessed. Old test counts and review revisions do not establish its status.
- The [Modbus protocol PDF](reference/Sigenergy%20Modbus%20Protocol%20EN_V2.9.pdf) describes vendor contracts; installed-firmware behavior still requires device verification. See [verification scope](reviews/verification.md).
