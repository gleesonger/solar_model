"""Shared All/None controls with real Analysis charts and synthetic data."""

from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_chart_series_selection_and_tariff_switching(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "charts.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from datetime import date
import pandas as pd
from nicegui import ui
from solar_model.dashboard.components.charts import render_historical_charts
from solar_model.dashboard.state import DashboardState

@ui.page('/')
def index():
    frame = pd.DataFrame({{"period": ["00:00", "01:00"], **{{key: [1., 2.] for key in (
        "solar", "load", "house_load", "forecast_total", "forecast_raw_total",
        "grid_import", "grid_export", "grid_import_cost", "grid_export_revenue",
        "net_cost", "no_solar_battery_import_cost", "import_rate", "export_rate",
        "import_cost_tariff__day", "import_cost_tariff__night")}}}})
    power = frame.assign(time=frame.period, battery=1., inverter=1.)
    battery = pd.DataFrame({{"time": frame.period, "available_energy_kwh": [3., 4.], "soc_percent": [30., 40.]}})
    charts = render_historical_charts(frame, power, battery, DashboardState(date(2026, 10, 26)), (), {{}}, lambda event: None)
    for name in ("energy", "power", "money", "battery", "array_energy"):
        display = getattr(charts, name)
        display.title.props(f"data-testid={{name}}-title")
        display.chart.props(f"data-testid={{name}}-chart")
    ui.button("Refresh data", on_click=lambda: charts.energy.update(frame.assign(house_load=3.)))

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
                        pytest.fail((tmp_path / "server.log").read_text(encoding="utf-8")[-4000:])
                    time.sleep(.1)
            else:
                pytest.fail("Synthetic dashboard did not start")
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
                page.get_by_test_id("energy-title").wait_for()

                def header(name):
                    return page.get_by_test_id(f"{name}-title").locator("..")

                def check_selection(name, selected):
                    page.wait_for_function("""([name, selected]) => {
                        const chart = echarts.getInstanceByDom(document.querySelector(`[data-testid="${name}-chart"]`));
                        const option = chart?.getOption();
                        return option?.series.length && option.series.every(s =>
                            (option.legend[0].selected[s.name] !== false) === selected);
                    }""", arg=[name, selected])

                def cycle(name, state):
                    previous = {"None": "Default", "All": "None", "Default": "All"}[state]
                    header(name).get_by_role("button", name=previous, exact=True).click()
                    header(name).get_by_role("button", name=state, exact=True).wait_for()

                def selection(name):
                    return page.evaluate("""name => {
                        const option = echarts.getInstanceByDom(document.querySelector(`[data-testid="${name}-chart"]`)).getOption();
                        return Object.fromEntries(option.series.map(s => [s.name, option.legend[0].selected[s.name] !== false]));
                    }""", name)

                defaults = {name: selection(name) for name in ("energy", "power", "money")}
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1000})
                    for name in ("energy", "power", "money"):
                        cycle(name, "None")
                        check_selection(name, False)
                        cycle(name, "All")
                        check_selection(name, True)
                        cycle(name, "Default")
                        assert selection(name) == defaults[name]
                        box = header(name).bounding_box()
                        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
                    for name in ("battery", "array_energy"):
                        assert header(name).get_by_role("button", name="Default", exact=True).count() == 0

                cycle("energy", "None")
                page.get_by_role("button", name="Refresh data", exact=True).click()
                check_selection("energy", False)
                cycle("energy", "All")
                cycle("energy", "Default")
                assert selection("energy") == defaults["energy"]
                page.evaluate("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=power-chart]'))
                    .dispatchAction({type: 'dataZoom', start: 25, end: 75})""")
                cycle("power", "None")
                check_selection("power", False)
                assert page.evaluate("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=power-chart]')).getOption().dataZoom[0].start""") == 25

                cycle("money", "None")
                header("money").get_by_role("checkbox", name="Split tariff").locator(".q-checkbox__inner").click()
                page.wait_for_function("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=money-chart]')).getOption().series.some(s => s.name === 'Import Cost')""")
                check_selection("money", False)
                cycle("money", "All")
                header("money").get_by_role("checkbox", name="Split tariff").locator(".q-checkbox__inner").click()
                page.wait_for_function("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=money-chart]')).getOption().series.some(s => s.name === 'Import - day')""")
                check_selection("money", True)
                cycle("money", "Default")
                assert selection("money") == defaults["money"]
                header("money").get_by_role("checkbox", name="Split tariff").locator(".q-checkbox__inner").click()
                page.wait_for_function("""() => echarts.getInstanceByDom(document.querySelector('[data-testid=money-chart]')).getOption().series.some(s => s.name === 'Import Cost')""")
                cycle("money", "None")
                cycle("money", "All")
                cycle("money", "Default")
                assert selection("money")["Import Cost"] is True
                assert selection("money")["Net Cost"] is False
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
