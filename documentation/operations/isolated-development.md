# Isolated development walkthrough

Run from the repository root with dependencies installed as in [development and testing](development-and-testing.md). Ordinary launchers use the configured database/hardware; there is no built-in `--demo` mode.

## Server/data checks without hardware

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_review_recovery.py tests/test_grid_power_rollup.py tests/test_remaining_review_fixes.py tests/test_forecast_schedule.py
```

These tests create temporary databases and fake readings/requests. For new fixtures, use pytest `tmp_path`, `open_database(tmp_path / "fixture.db")`, and fake the provider/Modbus boundary. Never substitute the configured production path. For migration tests, create the legacy fixture first, then call the opener and check repeated opening.

## Full dashboard with synthetic data

The existing [review fixture launcher](../reviews/system-review-evidence/browser/server.py) seeds sample telemetry/forecasts/device metadata, replaces `LivePowerCollector`, and mocks Control recalculation/restart. It starts only the dashboard; no external forecast collector runs. Reuse it from a **fresh temporary copy**, leaving archived evidence intact:

```powershell
$projectRoot = (Get-Location).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$fixtureDirectory = Join-Path ([IO.Path]::GetTempPath()) ('solar-dev-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixtureDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'documentation\reviews\system-review-evidence\browser\server.py') -Destination (Join-Path $fixtureDirectory 'server.py')
& $projectPython -X utf8 (Join-Path $fixtureDirectory 'server.py')
```

Keep the working directory at the repository root: the launcher imports current source and reads `config.yaml`, then replaces the database path with its adjacent temporary `fixture.db` and binds `127.0.0.1:18091`. It supplies synthetic panels 1/2 and PV strings 1/2; adapt **the temporary copy** if the local configuration uses a different mapping. Nothing is seeded into `solar.db`.

Open `http://127.0.0.1:18091`. Check Recent, System Info, Analysis Apply, chart/table toggles, custom variables/legends and CSV/XLSX downloads. Use 1440×1000, 390×844 and 360×800 viewports. Persisted fixture records are dated today at 08:00–09:30; the freshness banner may legitimately be visible. Fake live values differ deliberately from persisted samples. Mocked Control records actions in temporary `control-actions.txt`; it proves UI behavior, not actual recalculation/restart recovery.

Stop with Ctrl+C, or from another terminal:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18091/__review_shutdown'
```

The shutdown endpoint belongs only to this loopback fixture. Generated database/action files stay under `$fixtureDirectory`; downloads use the browser's destination. After the process exits, the temporary fixture directory can be removed. Use a fresh directory to regenerate date-relative data on another day.

## Browser automation and boundaries

Install Playwright/browser separately if needed; the [warning browser tests](../../tests/test_dashboard_freshness_browser.py) exercise synthetic DOM/socket state. Full-app scripts under the archived review's `browser/` directory target port 18091 and may write results beside themselves: copy them into the temporary directory before adapting/running them. Some deliberately reproduce old defects and are not current passing regression tests.

This workflow verifies presentation against synthetic inputs. It does not exercise real register compatibility, provider limits, Docker deployment, persisted collector scheduling under real load, or actual administrative subprocesses. Use targeted temporary-database tests for those server contracts before controlled integration checks.
