"""Browser regressions; requires Playwright and a Chromium browser."""

from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")
SCRIPT = Path(__file__).resolve().parents[2] / "solar_model/dashboard/assets/warnings.js"


@pytest.fixture
def page(request):
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(channel="msedge", headless=True)
        except playwright.Error:
            try:
                browser = driver.chromium.launch(headless=True)
            except playwright.Error:
                pytest.skip("No Chromium browser installed")
        page = browser.new_page(viewport={"width": 390, "height": 844}, timezone_id=getattr(request, 'param', 'UTC'))
        page.set_content('''
            <div id="dashboard-warning-banner" role="alert">
                <span id="dashboard-warning-message"></span>
            </div>
            <div id="dashboard-warning-data" data-full-stale-after-seconds="180"
                 data-live-stale-after-seconds="15" data-heartbeat-timeout-ms="20000" hidden></div>
            <span class="live-update-indicator">
                <span class="live-update-dot"></span><span class="live-update-dot"></span><span class="live-update-dot"></span>
            </span>
            <div class="live-update-message">Last reading: 2026-10-19 12:00:00 UTC</div>
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
        page.add_style_tag(content=SCRIPT.with_name("live_updates.css").read_text(encoding="utf-8"))
        page.add_script_tag(content=SCRIPT.with_name("live_updates.js").read_text(encoding="utf-8"))
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


def test_socket_loss_requires_new_heartbeat_but_brief_tab_switch_does_not(page):
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
    page.evaluate("window.testNow += 1000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert page.locator("#dashboard-warning-banner").is_hidden()
    page.evaluate("window.testNow += 21000")
    page.evaluate("document.dispatchEvent(new Event('resume'))")
    assert "Waiting for server" in message(page)
    page.evaluate("window.freshReadings(); window.dashboardHeartbeat()")
    assert page.locator("#dashboard-warning-banner").is_hidden()


def test_indicator_expires_in_css_without_javascript_timeout(page):
    page.evaluate('''() => {
        window.dashboardHeartbeat();
        window.setTimeout = () => { throw new Error('Indicator must not schedule a timeout'); };
        window.flashLiveUpdateIndicator();
    }''')
    indicator = page.locator('.live-update-indicator')
    assert indicator.is_visible()
    assert page.locator('.live-update-message').is_visible()
    # Advance the browser animation timeline, without invoking any JS timer.
    page.evaluate("document.querySelector('.live-update-indicator').getAnimations()[0].finish()")
    assert indicator.is_hidden()
    # A fresh reading restarts the animation after the previous one has ended.
    page.evaluate("window.flashLiveUpdateIndicator()")
    assert indicator.is_visible()


def test_indicator_clears_on_wall_clock_expiry_and_disconnect(page):
    page.evaluate("window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    last_reading = page.locator('.live-update-message').inner_text()
    indicator = page.locator('.live-update-indicator')
    assert indicator.is_visible()
    page.evaluate('''() => {
        window.testNow += 4000;
        document.dispatchEvent(new Event('resume'));
    }''')
    assert indicator.is_hidden()
    page.evaluate("window.flashLiveUpdateIndicator()")
    assert indicator.is_visible()
    page.evaluate("window.socket.connected = false; window.socket.handlers.disconnect()")
    assert indicator.is_hidden()
    page.evaluate("window.flashLiveUpdateIndicator()")
    assert indicator.is_hidden()
    assert page.locator('.live-update-message').inner_text() == last_reading


@pytest.mark.parametrize('page, timestamp, expected', [
    ('Asia/Kolkata', '2026-10-19T22:45:12+00:00', '2026-10-20 04:15:12'),
    ('America/Los_Angeles', '2026-03-08T09:30:00+00:00', '2026-03-08 01:30:00'),
    ('America/Los_Angeles', '2026-03-08T10:30:00+00:00', '2026-03-08 03:30:00'),
], indirect=['page'])
def test_last_reading_uses_browser_local_time_without_timezone_suffix(page, timestamp, expected):
    page.evaluate('''timestamp => {
        document.getElementById('dashboard-warning-data').dataset.lastLiveUpdate = timestamp;
        window.tick();
    }''', timestamp)
    assert page.locator('.live-update-message').inner_text() == f'Last reading: {expected}'
    page.evaluate("document.getElementById('dashboard-warning-data').dataset.lastLiveUpdate = ''; window.tick()")
    assert page.locator('.live-update-message').inner_text() == 'Last reading: awaiting live data'


def test_indicator_clears_on_offline_freeze_and_stale_readings(page):
    page.evaluate("window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    indicator = page.locator('.live-update-indicator')
    page.context.set_offline(True)
    assert indicator.is_hidden()
    page.context.set_offline(False)
    page.evaluate("window.flashLiveUpdateIndicator()")
    assert indicator.is_hidden()  # Wait for a new heartbeat after going online.
    page.evaluate("window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    assert indicator.is_visible()
    page.evaluate("document.dispatchEvent(new Event('freeze'))")
    assert indicator.is_hidden()
    page.evaluate("window.testNow += 16000; window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    assert indicator.is_hidden()


def test_indicator_reduced_motion_expires_without_pulsing(page):
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    assert page.locator('.live-update-indicator').evaluate("node => getComputedStyle(node).animationName") == 'live-update-still'
    assert page.locator('.live-update-dot').first.evaluate("node => getComputedStyle(node).animationName") == 'none'
    page.evaluate("document.querySelector('.live-update-indicator').getAnimations()[0].finish()")
    assert page.locator('.live-update-indicator').is_hidden()


def test_fresh_live_updates_pulse_even_when_persisted_collection_is_stale(page):
    page.evaluate('''() => {
        document.getElementById('dashboard-warning-data').dataset.lastFullUpdate = '';
        window.dashboardHeartbeat();
        window.flashLiveUpdateIndicator();
    }''')
    assert 'Collected data is out of date' in message(page)
    assert page.locator('.live-update-indicator').is_visible()
    page.evaluate("window.testNow += 16000; window.dashboardHeartbeat(); window.flashLiveUpdateIndicator()")
    assert page.locator('.live-update-indicator').is_hidden()


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
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app_script = tmp_path / "warning_app.py"
    app_script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from datetime import datetime, timezone
from types import SimpleNamespace
import pandas as pd
from nicegui import ui
from solar_model.dashboard.components import warnings
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.state import DashboardElements
from solar_model.storage.queries.types import LivePowerData
from solar_model.dashboard.tabs.recent import RecentTab
warnings.HEARTBEAT_INTERVAL_SECONDS = 0.2
@ui.page('/')
def index():
    config = SimpleNamespace(timezone='Europe/Dublin', forecast=SimpleNamespace(solar=SimpleNamespace(arrays=())),
        actuals_to_forecast_panel_ids={{}}, dashboard=SimpleNamespace(actuals_to_forecast={{}}),
        actuals=SimpleNamespace(data_retrival_schedule=SimpleNamespace(
        full_updated_interval_seconds=60, live_update_interval_seconds=1,
    )))
    render_dashboard_styles()
    status = warnings.DashboardWarnings(config)
    timestamp = datetime.now(timezone.utc).isoformat()
    status.update_full(timestamp)
    status.update_live(timestamp)
    reading = LivePowerData(timestamp, timestamp, {{'solar': 2., 'battery_available_energy_kwh': 8., 'battery_soc_percent': 50.}})
    tab = RecentTab(config, SimpleNamespace(), DashboardElements(), SimpleNamespace(snapshot=lambda: reading), status)
    day = pd.DataFrame(columns=['solar', 'battery', 'load', 'grid_import', 'grid_export',
        'forecast_total', 'forecast_bod_total', 'forecast_raw_total'])
    tab.render_recent_tab((day, timestamp, reading, pd.DataFrame(), pd.DataFrame()))
    def next_reading():
        nonlocal reading
        from datetime import timedelta
        next_time = datetime.fromisoformat(reading.collected_at_utc) + timedelta(seconds=1)
        reading = LivePowerData(next_time.isoformat(), next_time.isoformat(), reading.values)
        tab.refresh_live_power()
    ui.button('Next reading', on_click=next_reading)
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
                pytest.fail("Warning test server did not start: " + (tmp_path / "server.log").read_text(encoding="utf-8"))
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
                label = page.locator('.live-update-message')
                assert label.inner_text().startswith('Last reading: ')
                for width in (390, 1280):
                    page.set_viewport_size({'width': width, 'height': 844})
                    bounds = label.bounding_box()
                    table_bounds = page.locator('.live-today-table').bounding_box()
                    assert bounds['y'] >= table_bounds['y'] + table_bounds['height']
                    assert bounds['x'] + bounds['width'] <= width
                    assert label.evaluate("node => getComputedStyle(node).fontSize") == '12px'
                    assert label.evaluate("node => getComputedStyle(node).color") == 'rgb(148, 163, 184)'
                original_reading = label.inner_text()
                page.get_by_role('button', name='Next reading', exact=True).click()
                page.wait_for_function("previous => document.querySelector('.live-update-message').textContent !== previous", arg=original_reading)
                indicator = page.locator('.live-update-indicator')
                assert indicator.is_visible(), indicator.evaluate("node => ({html: node.outerHTML, styles: getComputedStyle(node).cssText, animation: getComputedStyle(node).animationName})")
                dots = indicator.locator('.live-update-dot')
                assert dots.count() == 3
                for dot in dots.all():
                    assert dot.is_visible()
                    assert dot.bounding_box()['width'] >= 4
                    assert float(dot.evaluate("node => getComputedStyle(node).opacity")) >= 0.7
                # Brief tab switches preserve an otherwise healthy connection.
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
