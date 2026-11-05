# Dashboard idle CPU investigation — 5 November 2026

This is a local Windows investigation of the dashboard revision tested, not a Linux/container benchmark or a universal CPU expectation. Current ownership and scheduling contracts are in [dashboard implementation](../implementation/dashboard-ui.md).

The initial measured configuration included a database change monitor that has since been removed and one-second binding polling that has since changed to 0.25 seconds. The first investigation below is historical; the repeat comparison at the end includes the current configuration.

## Method

The real dashboard ran on an unused loopback port using the project's Python 3.12 virtual environment and a SQLite backup in a temporary directory. The original database and existing dashboard processes were untouched. Automatic browser opening was disabled. No browser socket connected, no full readings were inserted, and no projections ran. The normal persistent planning worker warmed up before measurement.

Each successful trace included a 20-second unprofiled interval measured with `time.process_time()` and a separate 20-second cProfile interval. Instrumented I/O callbacks counted marker/summary queries. Process CPU excludes the separate planning worker. cProfile adds overhead, and short Windows CPU-time samples are quantised and variable.

One preliminary multi-thread profiler attempt raised a profiler conflict; that profiled interval was discarded. The subsequent valid traces used one profiler and completed without application errors.

## Findings

Before the idle changes, the dashboard rebuilt an ORM marker query each second even when SQLite had no new commits. In a valid 20-second profiled interval this accounted for 20 queries and about 0.048 seconds of cumulative elapsed time. This is not an independent CPU measurement. The service already reused pooled connections after the earlier fix, but still constructed query/session machinery on each check.

NiceGUI's binding refresh loop and its unused implicit-page outbox each ran about 185 times in 20 seconds. The app uses an explicit per-browser root page, so this implicit client never connected. Uvicorn also wakes its maintenance loop about ten times per second. That server loop is retained.

Recent summaries and device information were loaded at startup and refreshed periodically without viewers. These refreshes did not fall within the short idle windows below, but the initial query and source inspection established the unnecessary work. Browser regression checks now verify subscriber-driven loading, no queries for plain HTTP probes, suspension after disconnect, and refreshing stale data on reconnect.

## Changes and verification

- The measured experiment used `DatabaseChangeMonitor` and `PRAGMA data_version` to avoid unchanged-database ORM queries. This experiment was subsequently removed at the user's request: it did not demonstrate a meaningful CPU improvement. Current planning checks query the latest full-reading timestamp every second using the cached connection pool.
- Recent snapshots wait without polling when no connected browser subscribes. The first browser loads data; fresh snapshots are reused and stale snapshots refresh on reconnect. Planning remains independent of browser activity.
- The unused implicit client's outbox is stopped. The measured trace used binding polling once per second; this was subsequently changed to four times per second (0.25 seconds) at the user's request. Real browser outboxes and direct UI updates remain active. The figures below describe the original measured setting and have not been remeasured at four times per second.

| Measured activity in a 20-second trace | Before | After |
| --- | ---: | ---: |
| ORM full-reading timestamp queries | 20 | 0 |
| Binding refresh iterations | 185 | 20 |
| Unused implicit-page outbox iterations | 185 | 0 |
| Profiled function calls | 59,790 | 22,224 |
| Uvicorn maintenance iterations | 185 | 186 |

The first startup timestamp lookup still runs to establish the collection baseline; it is outside the after-measurement interval. No summaries or device-information queries ran during the final no-browser session.

Unprofiled local samples ranged from approximately 0.23% to 0.55% of one core before, with 0.47% in the final sample. Profiled samples were approximately 0.63% before and 0.39% after. These short, noisy samples do not establish a precise CPU improvement; the verified result is less unnecessary work, including about 63% fewer profiled function calls. The final trace spent approximately 99.5% of elapsed wall time waiting in Windows' completion queue. Remaining regular activity was predominantly the server maintenance loop plus the one-second configuration/change checks.

Targeted storage/scheduler tests and real-dashboard browser checks passed using temporary data, including desktop/mobile rendering and disconnect/reconnect behavior. No claim is made that the container's reported 1% has been reproduced or eliminated; measure the rebuilt Linux image to establish that result.

## Repeat comparison after removing the database change monitor

The real dashboard was rerun locally in isolated source copies against identical SQLite backups. The initial configuration was taken from Git HEAD before the idle changes; the removed full optimisation was reconstructed in temporary copies. The working application source and production database were not modified. All variants used the same configuration, one persistent planning worker and no browser sockets, new readings or plan projections. Initial imports and worker warm-up were excluded.

Each configuration had a 60-second unprofiled process-CPU window, followed by a separate 15-second cProfile window. The matched four-binding-checks-per-second configurations were repeated in reverse order, with another 60-second unprofiled window each. Reported CPU is the dashboard process's total CPU time divided by elapsed time, expressed as a percentage of one core; it is not the host's total CPU percentage and excludes the idle worker process.

| Configuration | Binding checks/second | Change monitor | Unprofiled CPU, % of one core | Runs |
| --- | ---: | --- | ---: | ---: |
| Initial, before idle optimisation | 10 | No | 0.47 | 1 |
| Original full optimisation | 1 | Yes | 0.36 | 1 |
| Full optimisation with requested binding rate | 4 | Yes | 0.17 average; 0.13–0.21 | 2 |
| Current code after monitor removal | 4 | No | 0.25 average; 0.16–0.34 | 2 |

The initial minute included one unnecessary Recent summary refresh, consuming about 0.078 seconds of measured I/O-thread CPU. Its first 55 seconds, before that refresh, averaged 0.34% of one core. The optimised configurations performed no summary refreshes without viewers. This distinguishes the saved summary work from ordinary idle housekeeping.

At the same four-per-second binding rate, removing the monitor added approximately 0.08 percentage points of one-core CPU on average in these samples, roughly 0.05 CPU seconds per minute. The ranges overlap and only two runs of each variant were measured. This suggests, at most, a small absolute effect; it does not establish a large or reliably repeatable performance benefit for the monitor. It remains removed.

The apparent lower CPU at four binding checks per second than at one is not evidence that faster polling improves performance. The one-per-second variant was measured only once, Windows CPU accounting was quantised, and the measured workload was very small. These measurements should not be used to rank binding frequencies.

The current code issued 60 timestamp queries per minute; monitored variants issued 60 lightweight change-counter checks and no repeated ORM timestamp queries. The separate 15-second traces recorded 44,276 function calls initially, 16,585 with the original full optimisation, 19,576 with the full four-per-second configuration, and 33,510 with the current code. These call counts describe profiling overhead/work and are not CPU-speedup percentages.

Raw CPU samples, query counts, environment information, dataset hash and relevant source hashes are retained in [dashboard-idle-comparison.json](dashboard-idle-comparison.json). All six runs completed without application errors, browser connections or projections. The Linux container's reported CPU usage remains unmeasured locally.
