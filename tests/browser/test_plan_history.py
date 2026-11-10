"""Synthetic persisted-plan browsing on desktop and mobile, with no solver run."""
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_saved_plan_views_dates_navigation_and_export(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "history_app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
import cvxpy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pandas as pd
from nicegui import ui
from solar_model.config import load_config
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.plan_history import PlanHistoryTab
from solar_model.services.plan_history import persist_report
from solar_model.services.planning import PlanReport
from solar_model.storage.migrations import open_database
from tests.dashboard.test_plan import sample_result

config = load_config({str(repo / 'config.yaml')!r})
config = replace(config, database=replace(config.database, path={str(tmp_path / 'solar.db')!r}))
db = open_database(config.database.path)
result = sample_result()
base = result.results['med']
result.results.clear()
for load in ('low','med','high'):
    for solar in ('low','med','high'):
        name = 'med' if load == solar == 'med' else load + '_' + solar
        result.results[name] = base.copy()
stamp = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
for delta in (timedelta(days=-2), timedelta(minutes=-5), timedelta()):
    when = stamp + delta
    report = PlanReport(result, pd.DataFrame(), 20., config.energy_plan.scenarios, 3., 50., 20., when)
    persist_report(config, report, when, when)
db.close()

@ui.page('/')
def page():
    render_dashboard_styles()
    with ui.column().classes('w-full max-w-7xl mx-auto p-2 md:p-4'):
        ui.label('Plan History').classes('text-lg')
        history = PlanHistoryTab(config)
        history.render()
        ui.timer(.01, history.activate, once=True)
ui.run(host='127.0.0.1', port={port}, reload=False, show=False)
''', encoding="utf-8")
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(log_path.read_text(encoding="utf-8"))
                try:
                    urlopen(url, timeout=1).close()
                    break
                except Exception:
                    time.sleep(.2)
            else:
                pytest.fail("History test server did not start")
            with playwright.sync_playwright() as driver:
                browser = None
                for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
                    try:
                        browser = driver.chromium.launch(headless=True, **options)
                        break
                    except playwright.Error:
                        continue
                if browser is None:
                    pytest.skip("No Chromium browser installed")
                try:
                    for width in (1440, 390):
                        page = browser.new_page(viewport={"width": width, "height": 1000}, accept_downloads=True)
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.set_default_timeout(10000)
                        page.goto(url)
                        page.get_by_text("Calculated 2026-11-10 12:00:00", exact=False).wait_for()
                        assert page.get_by_role("table").count() == 1
                        page.get_by_role("button", name="Advanced", exact=True).click()
                        page.get_by_role("cell", name="0–24h", exact=True).wait_for()
                        with page.expect_download() as download:
                            page.get_by_role("button", name="Download Excel", exact=True).click()
                        assert download.value.suggested_filename.endswith("-advanced.xlsx")
                        page.get_by_role("button", name="Previous", exact=True).click()
                        page.get_by_text("Calculated 2026-11-10 11:55:00", exact=False).wait_for()
                        page.get_by_role("button", name="Next", exact=True).click()
                        page.get_by_text("Calculated 2026-11-10 12:00:00", exact=False).wait_for()
                        page.get_by_label("Date", exact=True).fill("2026-11-08")
                        page.get_by_role("button", name="Load date", exact=True).click()
                        page.get_by_text("Advanced details have expired", exact=False).wait_for()
                        page.get_by_role("button", name="Compact", exact=True).click()
                        page.get_by_role("cell", name="Total", exact=True).wait_for()
                        page.get_by_label("Date", exact=True).fill("2026-11-01")
                        page.get_by_role("button", name="Load date", exact=True).click()
                        page.get_by_text("No saved plans for this date", exact=False).wait_for()
                        assert page.get_by_role("button", name="Download Excel", exact=True).is_disabled()
                        page.get_by_role("button", name="Latest", exact=True).click()
                        page.get_by_text("Calculated 2026-11-10 12:00:00", exact=False).wait_for()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                        assert errors == []
                        page.close()
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
