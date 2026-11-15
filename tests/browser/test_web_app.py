"""Actual desktop install/launch lifecycle, without devices or a database."""
import json
from http.cookiejar import CookieJar
from pathlib import Path
import socket
import struct
import sys
import time
from urllib.request import HTTPCookieProcessor, build_opener

import pytest

from solar_model.runtime.processes import OwnedProcess

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("browser_channel", ["chromium", "chrome"])
@pytest.mark.parametrize("requires_login", [False, True])
def test_chrome_installs_and_launches_app(tmp_path, browser_channel, requires_login):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "install_dashboard.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from nicegui import app, ui
from solar_model.dashboard.web_app import INSTALL_HEAD_HTML, register_installation_assets
from solar_model.paths import package_path

register_installation_assets()

@app.middleware('http')
async def cookie_protected_assets(request: Request, call_next):
    protected = request.url.path == '/manifest.webmanifest' or request.url.path.startswith('/solar-app-')
    if {requires_login!r} and protected and request.cookies.get('install_session') != 'test':
        return RedirectResponse('/login')
    response = await call_next(request)
    if request.url.path == '/':
        response.set_cookie('install_session', 'test', httponly=True)
    return response

@app.get('/login')
def login():
    return HTMLResponse('<html><body>Login required</body></html>')

@ui.page('/')
def page():
    ui.add_head_html(INSTALL_HEAD_HTML)
    ui.label('Install test dashboard')

ui.run(host='127.0.0.1', port={port}, title='Solar dashboard', favicon=package_path('dashboard/assets/solar_power.svg'), reload=False, show=False)
''', encoding="utf-8")
    server = OwnedProcess([sys.executable, str(script)])
    base = f"http://127.0.0.1:{port}"
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with client.open(base + "/", timeout=1):
                    pass
                with client.open(base + "/manifest.webmanifest", timeout=1) as response:
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
            with client.open(base + icon["src"], timeout=2) as response:
                assert response.headers.get_content_type() == "image/png"
                image = response.read()
            assert image[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", image[16:24])
            assert icon["sizes"] == f"{width}x{height}"
        assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}

        with playwright.sync_playwright() as driver:
            try:
                browser = driver.chromium.launch_persistent_context(str(tmp_path / "browser-profile"), channel=browser_channel, headless=True)
            except playwright.Error:
                pytest.skip(f"No full {browser_channel} browser installed")
            try:
                for viewport in ({"width": 1440, "height": 1000}, {"width": 390, "height": 844}):
                    page = browser.new_page()
                    page.set_viewport_size(viewport)
                    page.goto(base)
                    assert page.locator('link[rel="manifest"]').get_attribute("href") == "/manifest.webmanifest"
                    assert page.locator('meta[name="theme-color"]').get_attribute("content") == "#ffffff"
                    session = page.context.new_cdp_session(page)
                    installed_manifest = session.send("Page.getAppManifest")
                    assert installed_manifest["errors"] == []
                    assert json.loads(installed_manifest["data"]) == manifest
                    diagnostics = session.send("Page.getInstallabilityErrors")
                    assert diagnostics["installabilityErrors"] == [], diagnostics
                # Install from the loaded page's manifest, not synthesized URL metadata.
                protocol = session
                identity = base + "/"
                installed = False
                try:
                    protocol.send("PWA.install", {"manifestId": identity})
                    installed = True
                    protocol.send("PWA.getOsAppState", {"manifestId": identity})
                    with browser.expect_page() as launched:
                        protocol.send("PWA.launch", {"manifestId": identity})
                    app_page = launched.value
                    app_page.wait_for_load_state()
                    assert app_page.url == identity
                    app_page.get_by_text("Install test dashboard", exact=True).wait_for()
                finally:
                    if installed:
                        protocol.send("PWA.uninstall", {"manifestId": identity})
            finally:
                browser.close()
    finally:
        server.stop()
