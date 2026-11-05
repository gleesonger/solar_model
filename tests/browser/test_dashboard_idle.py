"""The real dashboard sleeps without sockets and resumes its shared summaries."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_summaries_wait_for_browser_connections_and_resume_after_idle(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    fixture = (repo / "tests/browser/dashboard_fixture.py").read_text(encoding="utf-8")
    fixture = fixture.replace("port=18091", f"port={port}").replace("main_dashboard.load_config=lambda:cfg", "main_dashboard.load_config=lambda *args:cfg")
    fixture = fixture.replace("if __name__=='__main__': main_dashboard.main()", "")
    fixture += '''
from nicegui import ui
from nicegui.client import Client
from solar_model.dashboard.snapshot import DashboardSnapshotStore
stores = []
class TrackingSnapshots(DashboardSnapshotStore):
    calls = 0
    def __init__(self, *args):
        super().__init__(*args)
        stores.append(self)
    async def refresh(self):
        self.calls += 1
        return await super().refresh()
main_dashboard.DashboardSnapshotStore = TrackingSnapshots
original_run = ui.run
def no_browser(**kwargs):
    return original_run(**kwargs, show=False)
ui.run = no_browser
@app.get('/__review_idle_state')
def idle_state():
    store = stores[0]
    return dict(calls=store.calls, listeners=len(store._listeners),
        browsers=sum(client.has_socket_connection for client in Client.instances.values()))
@app.post('/__review_due')
def due():
    stores[0]._last_attempt = None
    stores[0]._wake.set()
    return {'ok': True}
if __name__=='__main__': main_dashboard.main()
'''
    script = tmp_path / "idle_dashboard.py"
    script.write_text(fixture, encoding="utf-8")
    logfile = tmp_path / "idle.log"
    with logfile.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"

            def state():
                with urlopen(url + "/__review_idle_state", timeout=2) as response:
                    return json.load(response)

            for _ in range(150):
                try:
                    initial = state()
                    break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail(logfile.read_text(encoding="utf-8")[-4000:])
                    time.sleep(.1)
            else:
                pytest.fail("Dashboard did not start")
            assert initial == {"calls": 0, "listeners": 0, "browsers": 0}
            # A plain HTTP probe must not subscribe as if it were a browser.
            with urlopen(url, timeout=2) as response:
                response.read()
            assert state() == initial
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.get_by_text("Live / Today", exact=True).wait_for()
                assert state()["calls"] == 1
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.screenshot(path=str(tmp_path / f"dashboard-{width}.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.close()
                for _ in range(100):
                    if state()["listeners"] == 0:
                        break
                    time.sleep(.05)
                else:
                    pytest.fail("Disconnected browser remained subscribed")
                with urlopen(Request(url + "/__review_due", method="POST"), timeout=2) as response:
                    response.read()
                assert state()["calls"] == 1
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.goto(url)
                page.get_by_text("Live / Today", exact=True).wait_for()
                for _ in range(100):
                    if state()["calls"] == 2:
                        break
                    time.sleep(.05)
                assert state()["calls"] == 2
                assert errors == []
                browser.close()
        finally:
            try:
                with urlopen(Request(url + "/__review_shutdown", method="POST"), timeout=2) as response:
                    response.read()
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
