"""Presentation checks with synthetic data; no production DB or devices."""
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_load_forecast_past_tables_hourly_legend_and_responsive_layout(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "load_dashboard.py"
    script.write_text(f'''import sys
sys.path.insert(0, {str(repo)!r})
from types import SimpleNamespace
import pandas as pd
from nicegui import ui
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.recent import RecentTab
from solar_model.dashboard.state import DashboardElements
from solar_model.storage.queries.types import LivePowerData
from solar_model.dashboard.components.chart_display import ChartDataDisplay
from solar_model.dashboard.components.charts import energy_chart_options
from solar_model.storage.queries.dashboard import ENERGY_SUMMARY_COLUMNS

@ui.page('/')
def index():
    render_dashboard_styles()
    config = SimpleNamespace(timezone="UTC", forecast=SimpleNamespace(arrays=()),
        dashboard=SimpleNamespace(actuals_to_forecast={{}}))
    reading = LivePowerData("2026-10-26T00:30:00Z", "2026-10-26T00:30:00Z", {{"load": .35, "battery_available_energy_kwh": 5., "battery_soc_percent": 50.}})
    tab = RecentTab(config, SimpleNamespace(), DashboardElements(), SimpleNamespace(snapshot=lambda: reading),
        SimpleNamespace(update_full=lambda *args: None, update_live=lambda *args: None))
    daily = pd.DataFrame([{{**dict.fromkeys(ENERGY_SUMMARY_COLUMNS), "period": "2026-10-23", "load": 4., "house_load": 3., "house_load_forecast": 24.}}])
    day = pd.DataFrame([{{**dict.fromkeys(["solar", "battery", "load", "grid_import", "grid_export", "forecast_total", "forecast_bod_total", "forecast_raw_total"], 0.), "house_load": 3., "house_load_forecast": 24.}}])
    tab.render_recent_tab((day, None, reading, daily, daily))
    baseline = pd.DataFrame({{"period": ["00:00", "01:00"], "solar": [1., 2.], "load": [2., 3.], "grid_import": [0., 0.], "grid_export": [0., 0.], "forecast_total": [1., 2.], "forecast_raw_total": [1., 2.]}})
    baseline["house_load"] = [1.8, 2.8]
    hourly = baseline.assign(house_load_forecast=[2.5, 3.5])
    display = ChartDataDisplay(dataframe=baseline, title="Energy by hour (kWh)",
        render_chart=lambda: ui.echart((energy_chart_options(baseline, "period") | {{"animation": False}})).classes("w-full h-96"),
        chart_options=lambda frame: energy_chart_options(frame, "period") | {{"animation": False}})
    display.chart.props("data-testid=energy-chart")
    ui.button("Hourly", on_click=lambda: display.update(hourly))
    ui.button("Daily", on_click=lambda: display.update(baseline))
    ui.button("Refresh", on_click=lambda: display.update(display.dataframe.assign(house_load_forecast=display.dataframe.house_load_forecast + 1)))
ui.run(host="127.0.0.1", port={port}, reload=False, show=False)
''', encoding="utf-8")
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urlopen(url, timeout=1):
                        break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail((tmp_path / "server.log").read_text(encoding="utf-8")[-5000:])
                    time.sleep(0.1)
            else:
                pytest.fail("Synthetic dashboard did not start: " + (tmp_path / "server.log").read_text(encoding="utf-8")[-5000:])
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                page = browser.new_page()
                page.goto(url)
                page.get_by_role("button", name="Hourly", exact=True).wait_for()
                page.wait_for_function("""() => {
                    const chart = echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]'));
                    const selected = chart?.getOption().legend[0].selected;
                    return selected?.['House Load'] === true && selected?.Load === false;
                }""")
                for width in (1440, 390, 360):
                    page.set_viewport_size({"width": width, "height": 1000})
                    assert page.evaluate("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().series.find(s => s.name === 'House Load').data""") == [1.8, 2.8]
                    assert page.locator(".live-today-table").get_by_text("House Load (Forecast)", exact=True).count() == 0
                    assert page.get_by_role("columnheader", name="House Load (Forecast)", exact=True).count() == 0
                    assert page.get_by_role("columnheader", name="House Load", exact=True).count() == 2
                    house = page.locator(".live-today-table").get_by_role("row").filter(has=page.get_by_role("cell", name="House Load", exact=True))
                    assert [value.strip() for value in house.get_by_role("cell").all_text_contents()] == ["House Load", "0.3", "3.0", "", "24.0", ""]
                    page.get_by_role("button", name="Hourly", exact=True).click()
                    # Inspect the actual browser legend, including updates.
                    page.wait_for_function("""() => {
                        const el = document.querySelector('[data-testid=energy-chart]');
                        const chart = el && echarts.getInstanceByDom(el);
                        return chart?.getOption().legend[0].selected['House Load (Forecast)'] === false;
                    }""")
                    assert page.locator(".live-today-table").bounding_box()["width"] <= width
                    expected_top = 125
                    page.wait_for_function("""top => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().grid[0].top === top""", arg=expected_top)
                    page.screenshot(path=str(tmp_path / f"load-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Daily", exact=True).click()
                    page.wait_for_function("""() => !echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().series.some(s => s.name === 'House Load (Forecast)')""")
                page.get_by_role("button", name="Hourly", exact=True).click()
                page.wait_for_function("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().series.some(s => s.name === 'House Load (Forecast)')""")
                page.evaluate("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).dispatchAction({type: 'legendSelect', name: 'House Load (Forecast)'})""")
                page.get_by_role("button", name="Refresh", exact=True).click()
                page.wait_for_function("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().series.find(s => s.name === 'House Load (Forecast)').data[0] === 3.5""")
                assert page.evaluate("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=energy-chart]')).getOption().legend[0].selected['House Load (Forecast)']""") is True
                page.screenshot(path=str(tmp_path / "load-enabled.png"), full_page=True)
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
