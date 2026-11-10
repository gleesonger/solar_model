"""Shared background plans reach multiple browsers without a manual refresh."""
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import json
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_shared_plan_demonstrations_and_publication_on_desktop_and_mobile(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_path = tmp_path / "config.yaml"
    config_path.write_text((repo / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "background_app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
import cvxpy
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pandas as pd
from nicegui import app, background_tasks, run, ui
from solar_model.config import load_config
from solar_model.dashboard import plan_store
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.plan import PlanTab
from solar_model.services.planning import PlanReport
from solar_model.services.plan_history import persist_report
from solar_model.storage.migrations import open_database
from solar_model.planning.types import NoFeasiblePlanError
from tests.dashboard.test_plan import sample_result

config = load_config({str(config_path)!r})
config = replace(config, database=replace(config.database, path={str(tmp_path / 'plans.db')!r}))
open_database(config.database.path).close()
store = plan_store.PlanStore(config, {str(config_path)!r})
marker = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
failure = False
calls = 0
plan_store.latest_full_update = lambda path: marker

async def calculate(callback, *args):
    global calls
    if callback.__name__ == 'warm_planning_worker':
        return 123
    configured, cutoff = args[:2]
    stamp = marker
    calls += 1
    await asyncio.sleep(.7 if configured.energy_plan.protection_preference == 80 else .15)
    if failure:
        raise NoFeasiblePlanError('synthetic missing plan')
    result = sample_result()
    for load in ['low', 'med', 'high']:
        for solar in ['low', 'med', 'high']:
            name = 'med' if load == solar == 'med' else load + '_' + solar
            result.results[name] = result.results['med'].copy()
    report = PlanReport(result, pd.DataFrame(), 20., configured.energy_plan.scenarios,
        configured.energy_plan.preferred_reserve_kwh, configured.energy_plan.protection_preference, 20., stamp)
    return persist_report(configured, report, cutoff, datetime.now(timezone.utc)) if callback.__name__ == 'generate_saved_plan' else report
run.cpu_bound = calculate

async def start():
    background_tasks.create(store.run_forever())
app.on_startup(start)
app.on_shutdown(store.stop)

@app.get('/test/state')
def state():
    latest = store.current
    return dict(version=store.version, calls=calls, running=store.running, ready=store._baseline_established,
        began=latest.began_at.isoformat() if latest else None,
        full_data=latest.full_data_at.isoformat() if latest else None,
        completed=latest.completed_at.isoformat() if latest else None)

@app.post('/test/pull')
async def pull():
    global marker
    marker += timedelta(minutes=5)
    await store.check_for_update()
    return {{'ok': True}}

@app.post('/test/fail')
async def fail():
    global failure
    failure = True
    return await pull()

@ui.page('/')
def page():
    render_dashboard_styles()
    client = ui.context.client
    with ui.column().classes('w-full max-w-7xl mx-auto p-2 md:p-4'):
        tab = PlanTab(config, {str(config_path)!r}, store)
        tab.render_plan_tab()
    unsubscribe = store.subscribe(lambda: client.safe_invoke(tab.apply_shared_plan))
    client.on_disconnect(unsubscribe)

if __name__ == '__main__':
    ui.run(host='127.0.0.1', port={port}, reload=False, show=False)
''', encoding="utf-8")
    logfile = tmp_path / "background.log"
    with logfile.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"

            def state():
                with urlopen(url + "/test/state", timeout=2) as response:
                    return json.load(response)

            for _ in range(150):
                try:
                    if state()["ready"]:
                        break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail(logfile.read_text(encoding="utf-8")[-4000:])
                time.sleep(.1)
            else:
                pytest.fail("Background plan scheduler did not start")
            assert state()["calls"] == 0 and state()["version"] == 0
            with urlopen(Request(url + "/test/pull", method="POST"), timeout=2) as response:
                response.read()
            for _ in range(100):
                if state()["version"] == 1:
                    break
                time.sleep(.1)
            else:
                pytest.fail("New full reading did not trigger the first background plan")
            initial = state()
            assert initial["calls"] == 1  # No browser has connected yet.
            assert initial["began"] <= initial["completed"] and initial["full_data"]
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                desktop = browser.new_page(viewport={"width": 1440, "height": 1000})
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                for page in (desktop, mobile):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.get_by_test_id("plan-action").wait_for()
                assert state()["calls"] == 1
                label = "[Demonstration - not part of actual plan, save config to disk to apply change]"
                assert not desktop.get_by_text(label, exact=True).is_visible()
                desktop.get_by_test_id("plan-solar-scenario").get_by_role("button", name="High", exact=False).click()
                mobile.get_by_role("button", name="Plan settings", exact=True).click()
                editor = mobile.get_by_role("textbox", name="YAML", exact=True)
                editor.fill(re.sub(r"(?m)^protection_preference:.*$", "protection_preference: 80.0", editor.input_value()))
                mobile.get_by_role("button", name="Run Demonstration", exact=True).click()
                mobile.get_by_text(label, exact=True).wait_for()
                assert mobile.get_by_text(label, exact=True).evaluate("element => getComputedStyle(element).color") == "rgb(185, 28, 28)"
                assert not desktop.get_by_text(label, exact=True).is_visible()
                assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                mobile.screenshot(path=str(tmp_path / "demonstration-mobile.png"), full_page=True)
                with urlopen(Request(url + "/test/pull", method="POST"), timeout=2) as response:
                    response.read()
                mobile.get_by_text(label, exact=True).wait_for(state="hidden")
                assert desktop.get_by_test_id("plan-solar-scenario").get_by_role("button", name="High", exact=False).get_attribute("aria-pressed") == "true"
                desktop.screenshot(path=str(tmp_path / "background-desktop.png"), full_page=True)
                with urlopen(Request(url + "/test/fail", method="POST"), timeout=2) as response:
                    response.read()
                for page in (desktop, mobile):
                    page.get_by_text("Plan refresh failed: synthetic missing plan The previous plan is still shown.", exact=True).wait_for()
                    assert page.get_by_test_id("plan-action").is_visible()
                assert state()["version"] == 2
                assert errors == []
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
