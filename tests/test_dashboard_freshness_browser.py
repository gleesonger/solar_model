"""Browser regressions; requires Playwright and a Chromium browser."""

from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")
SCRIPT = Path(__file__).resolve().parents[1] / "dashboard/assets/warnings.js"


@pytest.fixture
def page():
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(channel="msedge", headless=True)
        except playwright.Error:
            try:
                browser = driver.chromium.launch(headless=True)
            except playwright.Error:
                pytest.skip("No Chromium browser installed")
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_content('''
            <div id="dashboard-warning-banner" role="alert">
                <span id="dashboard-warning-message"></span>
            </div>
            <div id="dashboard-warning-data" data-full-stale-after-seconds="180"
                 data-live-stale-after-seconds="15" data-heartbeat-timeout-ms="20000" hidden></div>
        ''')
        page.evaluate('''() => {
            window.testNow = Date.now();
            Date.now = () => window.testNow;
            window.setInterval = callback => { window.tick = callback; };
            window.socket = {
                connected: true,
                handlers: {},
                on(event, callback) { this.handlers[event] = callback; },
                connect() { this.connectRequested = true; },
            };
            window.freshReadings = () => {
                const timestamp = new Date(Date.now()).toISOString();
                document.getElementById('dashboard-warning-data').dataset.lastFullUpdate = timestamp;
                document.getElementById('dashboard-warning-data').dataset.lastLiveUpdate = timestamp;
            };
            window.freshReadings();
        }''')
        page.add_script_tag(content=SCRIPT.read_text(encoding="utf-8"))
        page.evaluate("window.tick()")
        yield page
        browser.close()


def message(page):
    return page.locator("#dashboard-warning-message").inner_text()


def test_offline_warning_clears_only_after_new_server_heartbeat(page):
    page.evaluate("window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()
    page.context.set_offline(True)
    assert "Offline" in message(page)
    assert page.locator("#dashboard-warning-banner").is_visible()
    page.context.set_offline(False)
    assert "Waiting for server" in message(page)
    page.evaluate("window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()


def test_socket_loss_and_phone_resume_require_new_heartbeat(page):
    page.evaluate("window.dashboardHeartbeat()")
    page.evaluate('''() => {
        window.socket.connected = false;
        window.socket.handlers.disconnect();
    }''')
    assert "Connection lost" in message(page)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.evaluate('''() => {
        window.socket.connected = true;
        window.socket.handlers.connect();
    }''')
    assert "Waiting for server" in message(page)
    page.evaluate("window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert "Waiting for server" in message(page)
    page.evaluate("window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()


def test_server_silence_is_detected_even_with_fresh_readings(page):
    page.evaluate("window.dashboardHeartbeat()")
    page.evaluate('''() => {
        window.testNow += 21000;
        window.freshReadings();
        window.tick();
    }''')
    assert "Waiting for server" in message(page)
    page.evaluate("window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()


def test_stale_or_missing_readings_remain_flagged_with_live_connection(page):
    page.evaluate('''() => {
        window.testNow += 16000;
        window.dashboardHeartbeat();
    }''')
    assert "Live readings are unavailable or stale" in message(page)

    page.evaluate('''() => {
        window.testNow += 180000;
        window.dashboardHeartbeat();
    }''')
    assert "Collected data is out of date" in message(page)
    page.evaluate('''() => {
        window.freshReadings();
        window.tick();
    }''')
    assert page.locator("#dashboard-warning-banner").is_hidden()
    page.evaluate('''() => {
        document.getElementById('dashboard-warning-data').dataset.lastLiveUpdate = '';
        window.tick();
    }''')
    assert "Live readings are unavailable or stale" in message(page)


def test_warning_flow_in_running_nicegui(tmp_path):
    """Exercise actual Python timestamp updates and the server heartbeat API."""
    repo = SCRIPT.parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app_script = tmp_path / "warning_app.py"
    app_script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from datetime import datetime, timezone
from types import SimpleNamespace
from nicegui import ui
from dashboard import warnings
warnings.HEARTBEAT_INTERVAL_SECONDS = 0.2
@ui.page('/')
def index():
    config = SimpleNamespace(actuals=SimpleNamespace(data_retrival_schedule=SimpleNamespace(
        full_updated_interval_seconds=60, live_update_interval_seconds=1,
    )))
    status = warnings.DashboardWarnings(config)
    timestamp = datetime.now(timezone.utc).isoformat()
    status.update_full(timestamp)
    status.update_live(timestamp)
    ui.button('Error check', on_click=lambda: warnings.notify_error('Test action failed'))
ui.run(host='127.0.0.1', port={port}, reload=False, show=False)
''', encoding="utf-8")
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(app_script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urlopen(url, timeout=1):
                        break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail((tmp_path / "server.log").read_text(encoding="utf-8"))
                    time.sleep(0.1)
            else:
                pytest.fail("Warning test server did not start")
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.on("pageerror", lambda error: print("Browser error:", error))
                page.goto(url)
                page.wait_for_function("document.getElementById('dashboard-warning-banner')?.hidden === true", timeout=5000)
                assert page.locator("#dashboard-warning-data").get_attribute("data-last-live-update")
                assert page.locator("#dashboard-warning-banner").count() == 1
                # A resumed tab needs a new periodic heartbeat as well as the handshake.
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_function("document.getElementById('dashboard-warning-banner')?.hidden === true", timeout=5000)
                page.get_by_role("button", name="Error check").click()
                page.get_by_text("Test action failed", exact=True).wait_for()
                page.get_by_text("Dismiss", exact=True).click()
                page.context.set_offline(True)
                page.wait_for_function("document.getElementById('dashboard-warning-message').textContent.includes('Offline')")
                assert page.locator("#dashboard-warning-banner").is_visible()
                page.context.set_offline(False)
                page.wait_for_function("document.getElementById('dashboard-warning-banner')?.hidden === true", timeout=5000)
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
