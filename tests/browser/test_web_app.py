"""Browser manifest discovery and install assets, without devices or a database."""
import json
from pathlib import Path
import socket
import struct
import sys
import time
from urllib.request import urlopen

import pytest

from solar_model.runtime.processes import OwnedProcess

playwright = pytest.importorskip("playwright.sync_api")


def test_chrome_discovers_standalone_manifest_and_install_icons(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "install_dashboard.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from nicegui import ui
from solar_model.dashboard.web_app import INSTALL_HEAD_HTML, register_installation_assets
from solar_model.paths import package_path

register_installation_assets()

@ui.page('/')
def page():
    ui.add_head_html(INSTALL_HEAD_HTML)
    ui.label('Install test dashboard')

ui.run(host='127.0.0.1', port={port}, title='Solar dashboard', favicon=package_path('dashboard/assets/solar_power.svg'), reload=False, show=False)
''', encoding="utf-8")
    server = OwnedProcess([sys.executable, str(script)])
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urlopen(base + "/manifest.webmanifest", timeout=1) as response:
                    assert response.headers.get_content_type() == "application/manifest+json"
                    assert response.headers["Cache-Control"] == "no-cache"
                    manifest = json.load(response)
                break
            except OSError:
                if server.poll() is not None:
                    pytest.fail("Install test server exited")
                time.sleep(.1)
        else:
            pytest.fail("Install test server did not start")

        assert manifest["display"] == "standalone"
        assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
        for icon in manifest["icons"]:
            with urlopen(base + icon["src"], timeout=2) as response:
                assert response.headers.get_content_type() == "image/png"
                image = response.read()
            assert image[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", image[16:24])
            assert icon["sizes"] == f"{width}x{height}"
        assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}

        with playwright.sync_playwright() as driver:
            try:
                browser = driver.chromium.launch(headless=True)
            except playwright.Error:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    pytest.skip("No Chromium browser installed")
            try:
                for viewport in ({"width": 1440, "height": 1000}, {"width": 390, "height": 844}):
                    page = browser.new_page(viewport=viewport)
                    page.goto(base)
                    assert page.locator('link[rel="manifest"]').get_attribute("href") == "/manifest.webmanifest"
                    assert page.locator('meta[name="theme-color"]').get_attribute("content") == "#ffffff"
                    session = page.context.new_cdp_session(page)
                    installed_manifest = session.send("Page.getAppManifest")
                    assert installed_manifest["errors"] == []
                    assert json.loads(installed_manifest["data"]) == manifest
                    page.close()
            finally:
                browser.close()
    finally:
        server.stop()
