"""Holiday editor and built-in grid scrolling on desktop and mobile."""
from datetime import date, timedelta
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from solar_model.storage.migrations import open_database

playwright = pytest.importorskip("playwright.sync_api")


def test_holiday_grid_opens_at_latest_period(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "control.db"
    database = open_database(database_path)
    first = date(2026, 1, 1)
    database.replace_holiday_periods([((first + timedelta(days=index)).isoformat(), (first + timedelta(days=index)).isoformat()) for index in range(25)])
    database.close()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "control_app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from dataclasses import replace
from nicegui import ui
from solar_model.config import load_config
from solar_model.dashboard.tabs.control import ControlTab

config = load_config({str(repo / 'config.yaml')!r})
config = replace(config, database=replace(config.database, path={str(database_path)!r}))

@ui.page('/')
def page():
    tab = ControlTab(config)
    with ui.tabs(on_change=lambda e: ui.timer(.1, tab.show_latest_holiday, once=True)) as tabs:
        control = ui.tab('Control')
    with ui.tab_panels(tabs, value=control):
        with ui.tab_panel(control):
            tab.render_control_tab()
    ui.timer(.1, tab.show_latest_holiday, once=True)

ui.run(host='127.0.0.1', port={port}, reload=False, show=False)
''', encoding="utf-8")
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urlopen(url, timeout=1).close()
                    break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail(log_path.read_text(encoding="utf-8")[-3000:])
                    time.sleep(.1)
            else:
                pytest.fail("Control preview server did not start")
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                for width in (1440, 390):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    page.goto(url)
                    page.get_by_text("Backfill household-load forecasts", exact=True).wait_for()
                    page.wait_for_function("document.querySelector('.ag-row[row-index=\"24\"]') !== null")
                    assert page.get_by_text("2026-01-25", exact=True).first.is_visible()
                    assert page.get_by_role("button", name="Save holidays").count() == 0
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                    if width == 1440:
                        page.get_by_role("button", name="Add period").click()
                        page.wait_for_function("document.querySelector('.ag-row[row-index=\"25\"]') !== null")
                        page.locator('.ag-row[row-index="25"] [col-id="start_date"]').dblclick()
                        editor = page.locator('.ag-cell-inline-editing input')
                        assert editor.get_attribute("type") == "date"
                        editor.fill("2026-01-26")
                        assert editor.input_value() == "2026-01-26"
                        editor.press("Enter")
                        page.wait_for_function("document.querySelector('.ag-row[row-index=\"25\"] [col-id=\"end_date\"]')?.textContent.includes('2026-01-26')")
                        page.locator('.ag-row[row-index="25"] [col-id="end_date"]').dblclick()
                        editor = page.locator('.ag-cell-inline-editing input')
                        editor.fill("2026-01-24")
                        editor.press("Enter")
                        page.get_by_text("Holiday end date must not precede start date", exact=False).wait_for()
                    page.close()
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
    database = open_database(database_path)
    try:
        assert database.list_holiday_periods()[-1] == ("2026-01-26", "2026-01-26")
    finally:
        database.close()
