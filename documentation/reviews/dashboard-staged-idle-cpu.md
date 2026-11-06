# Staged dashboard idle CPU investigation — 5 November 2026

This Windows investigation measures idle usage after work has finished. It does not change production code or establish Linux/container performance. Terra executed the staged experiments; the parent agent reviewed the harness, rejected invalid attempts, inspected raw measurements and ran an independent callback diagnostic.

The final clean comparison measured **0.21% of one CPU core for Hello World NiceGUI and 0.91% for the dashboard**. Earlier dashboard minutes ranged from 0.03% to 0.36%. There is measurable framework housekeeping and variable application overhead; these samples do not identify a single cause for the higher dashboard minute. Sleeping planning workers and collectors had no measurable CPU in their sampled idle windows.

Raw process CPU bins, task boundaries, counters, process IDs, source/configuration hashes, rejected-attempt notes and diagnostic results are retained in [dashboard-staged-idle-cpu.json](dashboard-staged-idle-cpu.json). The earlier [idle investigation](dashboard-idle-cpu.md) describes different experiments and is historical context.

## Method and scope

The tested source revision was `6e0b43bc40936ddeb42921e7f4af0a7b2ac2db56`, with Python 3.12.10 and NiceGUI 2.24.2 on Windows 11, using an Intel i7-1280P (20 logical processors). Each stage ran on a dedicated loopback port without automatically opening a browser. A read-only SQLite backup supplied the temporary database; migrations, telemetry writes, forecast writes and calibration changes affected only that copy. Existing user processes were left running and untouched.

Each unprofiled window began at least 31 seconds after the relevant startup or job completion and lasted 60 seconds, sampled in twelve five-second bins. Windows `GetProcessTimes` supplied kernel plus user CPU across all threads of each identified process. Results are percentages of **one core**, not percentages of total machine capacity. Planning worker and collector CPU were sampled separately and added to the combined figure. Where sampled, the Windows virtual-environment launcher had no measurable CPU. Observed accounting increments were 0.015625 CPU seconds, corresponding to about 0.026 percentage points over a minute. Zero means no measurable increment, not proof of no instructions executed.

Readiness used a FastAPI health endpoint, avoiding NiceGUI page creation. Browser socket counts and task/query counters were captured at window boundaries; later harnesses also counted unexpected HTTP requests. Early runs did not independently count unexpected HTTP, although the harness initiated only health requests. Heavy work never intentionally overlapped an accepted idle window. Component stages progressively assembled real services around a minimal page; the final stages invoked the actual dashboard entry point and its real scheduler.

## Incremental results

All figures below are combined percentages of one core; the dashboard parent accounts for the measurable CPU. Component figures are single samples and should not be interpreted as additive costs or reliable rankings.

| Stage | Components or completed work | Idle CPU | Verification |
| --- | --- | ---: | --- |
| 1 | Stock implicit-page Hello World NiceGUI | 0.23% | No browser connection |
| 2 | Hello World with explicit root page, unused implicit outbox stopped, binding interval 0.25 seconds | 0.23% | No browser connection |
| 3 | Application imports, configuration and temporary database initialisation | 0.16% | No services running |
| 4 | Add one persistent, warmed planning worker | 0.26% | Worker had no measurable idle CPU |
| 5 | Add once-per-second configuration checks | 0.21% | 60 checks; no database timestamp polls |
| 6 | Add direct database timestamp polling | 0.21% | 60 timestamp queries and configuration checks; no projections |
| 7 | Complete one Recent snapshot refresh, then run its no-subscriber loop | 0.18% | Snapshot count remained one; no connected browser |
| 8 | Complete a real scheduled Modbus collection into the temporary database | 0.31% | Collector and worker had no measurable idle CPU |
| 9 | Complete scheduled collection and stored-data solar forecast adjustment | 0.29% | Eleven adjusted solar rows; collector and worker had no measurable idle CPU |
| 10 | Complete configured DP projection in the persistent worker | 0.31% | DP completed; persistent worker had no measurable idle CPU |
| 11 | Connect browser, receive live data, load Analysis, disconnect | 0.34% | Zero sockets/listeners; live reads, refreshes and projections remained unchanged |

Stage 7 intentionally forces a refresh to test residual activity after use. Normal no-browser startup does not perform that refresh. Stages 8 and 9 wait for a real configured collection boundary and then suspend subsequent collector cycles, isolating residual CPU after work; they do not measure the ongoing one-minute collection workload. Modbus access consisted of reads, with no device-control commands.

The first stage-9 attempt successfully fetched provider data (22 rows per array) and saved eleven adjusted solar rows, but its idle sample was rejected after a harness state-file error. The valid retry respected the resulting forecast reservation and skipped another provider request. Its explicit stored-data solar adjustment replay saved eleven rows. The household forecast helper returned zero new rows, consistent with reusing an existing daily profile; this does not verify a fresh household-model fit in that stage.

The first configured DP attempt exceeded the repository's five-second simulation cap and was terminated; it supplied no post-projection idle result. The user then explicitly authorised one run with a 120-second cap. This run used the real shared-plan refresh in the persistent worker and completed all four refinement stages: the DP log reported 3.990 seconds, including 2.723 seconds classified by the model as compilation and 1.267 seconds projection. After full report completion, the controller settled for 31.016 seconds and sampled 60 seconds. The parent used 0.1875 CPU seconds (0.3125% of one core); the worker and launcher had no measurable CPU. Projection count stayed one, with no live reads, snapshots, connected sockets or unexpected HTTP during measurement. Both server and worker exited normally. This verifies post-solve idle for this completed configured run, without implying an identical cold-start duration on other runs.

## Repetition and isolation

| Configuration | First observation | Reverse-order observation | Final clean observation |
| --- | ---: | ---: | ---: |
| Stock Hello World | 0.234% | 0.156% | 0.208% |
| Hello World with dashboard runtime options | 0.234% | 0.104% | — |
| Actual dashboard, no browser | 0.026% | 0.365% | 0.911% |
| Actual dashboard after browser disconnect | 0.339% | — | — |

The reverse-order column has incomplete process-tree isolation: a benchmark worker remained after its server exited, and exact server teardown timing was not recorded. The worker showed no measurable CPU in a later five-second check, but its CPU during those earlier windows was not sampled. Those observations are retained with this qualification rather than used as clean causal comparisons. The valid browser-disconnect window preceded this cleanup problem.

The final clean stock/dashboard pair held process handles through teardown and verified server and worker exit before another run. The dashboard's higher minute was uneven: several five-second bins accumulated 0.109–0.125 CPU seconds, while others recorded zero. Its 59 plan checks, zero sockets/listeners and unchanged zero counts for projections, live reads and refreshes rule out those heavy activities during this window. They do not establish which ordinary callback or native/thread activity caused the difference. The unusually low first dashboard sample also passed its counter checks and remains in the record.

## Callback diagnostic

Separate 15-second cProfile runs used `thread_time` but produced impossible timings, including negative entries and approximately 40 seconds of self time inside a 15-second trace. Their timing rankings were rejected. Call counts alone are not CPU percentages.

An independent diagnostic subsequently wrapped event-loop callbacks with Windows `QueryThreadCycleTime` and measured timestamp queries separately inside their I/O threads. It ran after 31 seconds settling, outside all unprofiled CPU windows. The main recurring callbacks in 15.003 seconds were:

| Activity | Observed callbacks | Interpretation |
| --- | ---: | --- |
| Uvicorn server maintenance | 140 | Approximately ten maintenance ticks per second |
| NiceGUI binding refresh | 58 | Approximately four refreshes per second |
| PlanStore coroutine resumptions | 30 | Fifteen checks, each resuming around asynchronous work |
| Timestamp database queries in I/O threads | 15 | One per second |

Server maintenance, PlanStore resumptions, sleep-completion callbacks and binding refresh accounted for most measured event-loop callback cycles. The fifteen database queries consumed about **25 ms combined elapsed time**, with measurable cycle counts but zero increment in the coarse thread-CPU counter. Their elapsed time is not a CPU measurement or an explanation of the earlier 0.91% minute.

This instrumentation excludes native event-loop waiting/dispatch work and other threads except the explicitly wrapped database calls. Callback cycles include instrumentation effects and cannot be converted directly to total-process CPU percentages. No hidden collection, snapshot or projection activity appeared. The diagnostic server and worker both exited gracefully without forced cleanup.

## Interpretation and remaining work

The application becomes quiet after the measured collection, solar adjustment and browser activity: collectors/workers sleep, Recent refreshes stop without subscribers, and live Modbus reads stop after disconnect. The dashboard still has recurring server ticks, four-per-second binding checks, and once-per-second configuration/database checks. The database change monitor remains removed.

The final clean pair demonstrates that a dashboard minute can cost more than plain NiceGUI, but the broad variation across runs prevents assigning a reliable CPU penalty to one feature. No production optimisation was applied on the basis of these measurements. A claim that configuration checks, database polling or binding frequency explains the entire reported drain would exceed the evidence. The Linux container requires its own matched steady-state measurement and native/thread-level attribution if the precise source of its CPU usage is needed.

The original temporary harness, logs and binary profiles remain in `C:\Users\glees\AppData\Local\Temp\solar-steady-1db7ff7a7ef144e8b7098f3a0122e51a`; the durable JSON contains the reviewed measurements and limitations. Rejected runs include a readiness probe that created a NiceGUI client, an insufficient post-disconnect settling interval, state-file failures and aborted setup attempts. One browser closure emitted a Windows connection-reset traceback; disconnection and subsequent idle counters still completed correctly. These harness issues and the orphan process do not establish a production shutdown defect.
