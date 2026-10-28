"""Plan controls, async feedback and chart/table presentation on desktop/mobile."""
from pathlib import Path
import socket
import re
import subprocess
import sys
import time
from zipfile import ZipFile
from urllib.request import urlopen

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_plan_refresh_scenarios_loading_and_failure(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "plan_app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
import asyncio
import pandas as pd
from nicegui import ui, run
from solar_model.config import load_config
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.plan import PlanTab
from solar_model.services.planning import PlanReport
from tests.dashboard.test_plan import sample_result

async def calculate(callback, config, as_of, preference):
    await asyncio.sleep(.7)
    if preference == 99:
        raise ValueError('Synthetic forecast unavailable')
    result = sample_result()
    for load in ['low', 'base', 'high']:
        for solar in ['low', 'base', 'high']:
            name = 'base' if load == solar == 'base' else load + '_' + solar
            if name not in result.results:
                result.results[name] = result.results['base'].copy()
    as_of = as_of.replace(hour=10, minute=5, second=0, microsecond=0)
    for name, frame in result.results.items():
        frame = pd.concat([frame] * 10, ignore_index=True)
        frame['starts_at'] = pd.date_range(as_of, periods=20, freq='15min')
        frame['ends_at'] = frame['starts_at'] + pd.Timedelta(minutes=15)
        frame['net_cost'] += (preference - 75) / 100
        load, solar = ('base', 'base') if name == 'base' else name.split('_')
        frame['solar_kwh'] *= dict(low=.5, base=1., high=1.5)[solar]
        frame['load_kwh'] *= dict(low=.5, base=1., high=1.5)[load]
        result.results[name] = frame
    stamp = result.results['base'].starts_at.iloc[0]
    actuals = pd.DataFrame(dict(timestamp=[stamp - pd.Timedelta(hours=1), stamp], solar=[2.,2.], house_load=[1.,1.], battery=[4.,4.], grid_import=[.3,.4], grid_export=[.1,0.]))
    return PlanReport(result, actuals, 20.)
run.cpu_bound = calculate

@ui.page('/')
def page():
    render_dashboard_styles()
    with ui.column().classes('w-full max-w-7xl mx-auto p-2 md:p-4'):
        with ui.element('div').classes('dashboard-tabs-row grid w-full grid-cols-[1fr_auto_1fr]'):
            ui.element('div')
            with ui.tabs().classes('solar-tabs') as tabs:
                for name in ['Recent', 'Analysis', 'Plan', 'System Info', 'Control']:
                    ui.tab(name)
        with ui.tab_panels(tabs, value='Plan').classes('w-full'):
            with ui.tab_panel('Plan').classes('p-0 min-w-0'):
                PlanTab(load_config()).render_plan_tab()

if __name__ == '__main__':
    ui.run(host='127.0.0.1', port={port}, reload=False, show=False)
''', encoding="utf-8")
    logfile = tmp_path / "server.log"
    with logfile.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(script)], cwd=repo, stdout=log, stderr=log)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urlopen(url, timeout=1).close()
                    break
                except OSError:
                    if process.poll() is not None:
                        pytest.fail(logfile.read_text(encoding="utf-8")[-4000:])
                    time.sleep(.1)
            else:
                pytest.fail("Plan preview server did not start")
            with playwright.sync_playwright() as driver:
                try:
                    browser = driver.chromium.launch(channel="msedge", headless=True)
                except playwright.Error:
                    try:
                        browser = driver.chromium.launch(headless=True)
                    except playwright.Error:
                        pytest.skip("No Chromium browser installed")
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                refresh = page.get_by_role("button", name="Refresh plan", exact=True)
                refresh.wait_for()
                adjustment = page.locator('input[type="number"]')
                adjustment.fill("75")
                refresh.click()
                page.get_by_text("Calculating plan…", exact=True).wait_for()
                assert not refresh.is_visible()
                assert adjustment.is_disabled()
                refresh.wait_for(state="visible")
                page.get_by_text("Action for next 15 mins - Self-consumption", exact=True).wait_for()
                assert refresh.is_disabled()
                adjustment.fill("25")
                page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(e => e.textContent.includes('Refresh plan')).disabled")
                adjustment.fill("75")
                page.wait_for_function("Array.from(document.querySelectorAll('button')).find(e => e.textContent.includes('Refresh plan')).disabled")
                assert page.get_by_text(re.compile("^Adjustment changed")).count() == 0
                assert page.get_by_text("Plan refreshed.", exact=True).count() == 0
                assert page.get_by_text(re.compile("^Plan from ")).count() == 0
                page.get_by_role("columnheader", name=re.compile("^Net")).wait_for()
                totals = page.get_by_text("Solar / House Load", exact=True)
                assert totals.bounding_box()["y"] < page.get_by_test_id("plan-chart").bounding_box()["y"]
                page.get_by_text("25.00 / 12.50 kWh", exact=True).wait_for()
                solar_buttons = page.get_by_test_id("plan-solar-scenario")
                load_buttons = page.get_by_test_id("plan-load-scenario")
                assert solar_buttons.get_by_role("button").count() == 3
                assert load_buttons.get_by_role("button").count() == 3
                assert solar_buttons.evaluate("element => element.tagName") == "FIELDSET"
                assert solar_buttons.get_by_role("button", name="Low (0.5x)", exact=True).evaluate("element => getComputedStyle(element).backgroundColor") == "rgb(245, 245, 245)"
                solar_buttons.get_by_role("button", name="Low (0.5x)", exact=True).click()
                page.get_by_text("12.50 / 12.50 kWh", exact=True).wait_for()
                solar_buttons.get_by_role("button", name="Base", exact=True).click()
                page.get_by_text("25.00 / 12.50 kWh", exact=True).wait_for()
                assert page.locator(".q-table tbody tr").count() == 7
                assert page.get_by_text("Records per page:", exact=True).count() == 0
                assert page.locator(".q-table tbody tr").last.locator("td").first.inner_text() == "Total"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(7).inner_text() == "25.00"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(0).inner_text() == "10:05"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(1).inner_text() == "11:00"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(4).inner_text() == "0.4000"
                page.get_by_role("columnheader", name="SOC (%)", exact=True).wait_for()
                expected_headers = ["Start", "End", "Action", "Tariff", "Import rate", "Export rate",
                                    "House", "Solar", "Loss", "Clipped", "Import", "Export",
                                    "Battery", "SOC", "Cost", "Revenue", "Net"]
                headers = page.get_by_role("columnheader").all_text_contents()
                assert page.locator(".plan-detail-table th.sortable").count() == 0
                assert len(headers) == len(expected_headers)
                assert all(expected in actual for expected, actual in zip(expected_headers, headers))
                compact_count = page.get_by_role("columnheader").count()
                page.get_by_role("button", name="Advanced", exact=True).click()
                page.get_by_role("columnheader", name="Battery start (kWh)", exact=True).wait_for()
                assert page.locator(".q-table tbody tr").count() == 21
                assert page.get_by_role("columnheader").count() > compact_count
                page.get_by_role("button", name="Compact", exact=True).click()
                page.wait_for_function("!Array.from(document.querySelectorAll('th')).some(e => e.textContent.includes('Battery start'))")
                with page.expect_download() as download_info:
                    page.get_by_role("button", name="Download Excel", exact=True).click()
                download = download_info.value
                assert download.suggested_filename.startswith("plan-base-")
                assert download.suggested_filename.endswith(".xlsx")
                with ZipFile(download.path()) as workbook:
                    assert 'name="Plan detail"' in workbook.read("xl/workbook.xml").decode()
                    sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
                    assert 'r="A8"' in sheet
                    texts = sheet + (workbook.read("xl/sharedStrings.xml").decode() if "xl/sharedStrings.xml" in workbook.namelist() else "")
                    assert "Action" in texts and "Self-consumption" in texts and "Total" in texts
                    assert "SOC (%)" in texts
                page.get_by_role("columnheader", name="Start", exact=True).click()
                assert page.locator(".q-table tbody tr").last.locator("td").first.inner_text() == "Total"
                assert re.fullmatch(r"\d{2}:\d{2}", page.locator(".q-table tbody tr").first.locator("td").first.inner_text())
                page.wait_for_function("echarts.getInstanceByDom(document.querySelector('[data-testid=plan-chart]'))?.getOption().series.length === 10")
                assert page.get_by_test_id("plan-chart").evaluate("""element => {
                    const chart = echarts.getInstanceByDom(element);
                    const positions = [0, 1].map(i => chart.getModel().getComponent('yAxis', i).axis.scale.getTicks()
                        .map(tick => chart.convertToPixel({yAxisIndex: i}, tick.value)));
                    return positions[0].length === positions[1].length &&
                        positions[0].every((value, i) => Math.abs(value - positions[1][i]) < 1);
                }""")
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1100})
                    page.wait_for_timeout(200)
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                    for group in (solar_buttons, load_buttons):
                        box = group.bounding_box()
                        title = group.locator("legend").bounding_box()
                        assert abs(title["x"] + title["width"] / 2 - box["x"] - box["width"] / 2) < 2
                    box = page.get_by_test_id("plan-chart").bounding_box()
                    assert box["width"] > 200 and box["x"] + box["width"] <= width + 1
                    page.screenshot(path=str(tmp_path / f"plan-{width}.png"), full_page=True)
                    page.evaluate("""() => {
                        const spacer = document.createElement('div');
                        spacer.id = 'scroll-test-spacer'; spacer.style.height = '1200px';
                        document.body.append(spacer);
                        const table = document.querySelector('.plan-detail-table table');
                        window.scrollTo(0, table.getBoundingClientRect().top + window.scrollY + 100);
                    }""")
                    page.wait_for_function("Math.abs(document.querySelector('.plan-detail-table thead').getBoundingClientRect().top) < 2")
                    page.locator('.plan-detail-table .q-table__middle').evaluate('element => element.scrollLeft = 100')
                    assert page.evaluate("""() => {
                        const table = document.querySelector('.plan-detail-table table');
                        return Math.abs(table.querySelector('th').getBoundingClientRect().left -
                            table.querySelector('tbody td').getBoundingClientRect().left) < 2;
                    }""")
                    page.evaluate("""() => {
                        const table = document.querySelector('.plan-detail-table table');
                        window.scrollTo(0, table.getBoundingClientRect().bottom + window.scrollY + 30);
                    }""")
                    page.wait_for_function("document.querySelector('.plan-detail-table thead').getBoundingClientRect().bottom < 0")
                    page.evaluate("document.getElementById('scroll-test-spacer').remove(); window.scrollTo(0, 0)")
                load_buttons.get_by_role("button", name="Low (0.5x)", exact=True).click()
                solar_buttons.get_by_role("button", name="High (1.5x)", exact=True).click()
                page.wait_for_function("echarts.getInstanceByDom(document.querySelector('[data-testid=plan-chart]')).getOption().series[1].data[0][1] === 3")
                page.get_by_text("37.50 / 6.25 kWh", exact=True).wait_for()
                page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).dispatchAction({type:'legendUnSelect', name:'Solar'})")
                assert page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).getOption().legend[0].selected.Solar === false")
                page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).dispatchAction({type:'dataZoom', start:20, end:80})")
                load_buttons.get_by_role("button", name="Base", exact=True).click()
                solar_buttons.get_by_role("button", name="Base", exact=True).click()
                page.wait_for_function("echarts.getInstanceByDom(document.querySelector('[data-testid=plan-chart]')).getOption().series[1].data[0][1] === 2")
                assert page.get_by_test_id("plan-chart").evaluate("element => {const o = echarts.getInstanceByDom(element).getOption(); return o.legend[0].selected.Solar === false && o.dataZoom[0].start === 20 && o.dataZoom[0].end === 80;}")
                adjustment.fill("25")
                refresh.click()
                page.get_by_text("€-2.50", exact=True).wait_for()
                assert page.locator(".q-table tbody tr").count() == 7
                adjustment.fill("99")
                refresh.click()
                page.get_by_text("Plan refresh failed. The previous plan is still shown.", exact=True).wait_for()
                assert refresh.is_visible() and not adjustment.is_disabled()
                assert not refresh.is_disabled()
                assert page.get_by_test_id("plan-chart").is_visible()
                assert not errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
