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
    config_path = tmp_path / "config.yaml"
    config_path.write_text((repo / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "plan_app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
import asyncio
import cvxpy  # Import native solver dependencies before NiceGUI in this Windows test process.
from dataclasses import replace
import pandas as pd
from nicegui import ui, run
from solar_model.config import load_config
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.plan import PlanTab
from solar_model.services.planning import PlanReport
from tests.dashboard.test_plan import sample_result

async def calculate(config):
    from datetime import datetime, timezone
    as_of = datetime.now(timezone.utc)
    preference = config.energy_plan.protection_preference
    model = "MILP" if config.energy_plan.optimiser.model == "mix_integer_linear_prog" else "DP"
    assert model in ('MILP', 'DP')
    await asyncio.sleep(2.)
    if preference == 99:
        raise ValueError('Synthetic forecast unavailable')
    result = sample_result()
    for load in ['low', 'med', 'high']:
        for solar in ['low', 'med', 'high']:
            name = 'med' if load == solar == 'med' else load + '_' + solar
            if name not in result.results:
                result.results[name] = result.results['med'].copy()
    as_of = as_of.replace(year=2026, month=6, day=16, hour=10, minute=5, second=0, microsecond=0)
    for name, frame in result.results.items():
        frame = frame.assign(solar_to_load_kwh=0., solar_to_battery_kwh=0., solar_to_grid_kwh=0.,
                             grid_to_load_kwh=0., grid_to_battery_kwh=0., battery_to_load_kwh=0., battery_to_grid_kwh=0.)
        frame = pd.concat([frame] * 10, ignore_index=True)
        frame['starts_at'] = pd.date_range(as_of, periods=20, freq='15min')
        frame['ends_at'] = frame['starts_at'] + pd.Timedelta(minutes=15)
        frame['net_cost'] += (preference - 75) / 100
        frame['terminal_score'] = 0.
        frame['tariff_name'] = model
        load, solar = ('med', 'med') if name == 'med' else name.split('_')
        frame['solar_kwh'] *= getattr(config.energy_plan.scenarios.solar, solar).multiplier
        frame['load_kwh'] *= getattr(config.energy_plan.scenarios.house_load, load).multiplier
        result.results[name] = frame
        extra = frame.iloc[[-1] * 6].copy().reset_index(drop=True)
        extra['starts_at'] = pd.date_range(frame.ends_at.iloc[-1], periods=6, freq='4h')
        extra['ends_at'] = extra['starts_at'] + pd.Timedelta(hours=4)
        extra.loc[extra.index[-1], 'terminal_score'] = .5
        extra.loc[extra.index[-1], 'total_score'] += .5
        result.lookahead_results[name] = extra
    stamp = result.results['med'].starts_at.iloc[0]
    actuals = pd.DataFrame(dict(timestamp=[stamp - pd.Timedelta(hours=1), stamp], solar=[2.,2.], house_load=[1.,1.], battery=[4.,4.], grid_import=[.3,.4], grid_export=[.1,0.]))
    return dict(report=PlanReport(result, actuals, 20., config.energy_plan.scenarios, config.energy_plan.preferred_reserve_kwh, preference, 20.), began_at=as_of, completed_at=as_of)
import solar_model.dashboard.tabs.plan as plan_module
plan_module.demonstrate = calculate

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
                config = load_config()
                config = replace(config, energy_plan=replace(config.energy_plan, scenarios=replace(config.energy_plan.scenarios,
                    house_load=replace(config.energy_plan.scenarios.house_load,
                        low=replace(config.energy_plan.scenarios.house_load.low, multiplier=.5),
                        high=replace(config.energy_plan.scenarios.house_load.high, multiplier=1.5)))))
                config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, model="mix_integer_linear_prog"), scenarios=replace(config.energy_plan.scenarios,
                    solar=replace(config.energy_plan.scenarios.solar, low=replace(config.energy_plan.scenarios.solar.low, multiplier=.4)))))
                PlanTab(config, {str(config_path)!r}).render_plan_tab()

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
                settings = page.get_by_role("button", name="Plan settings", exact=True)
                settings.wait_for()
                assert page.get_by_role("button", name="Run Demonstration", exact=True).count() == 0

                def wait_for_idle():
                    page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(e => e.textContent.includes('Plan settings')).disabled")

                def change_preference(value, model_key=None, wait=True):
                    settings.click()
                    editor = page.get_by_role("textbox", name="YAML", exact=True)
                    text = re.sub(r"(?m)^protection_preference:.*$", f"protection_preference: {value}", editor.input_value())
                    if model_key is not None:
                        text = re.sub(r"(?m)^  model:.*$", f"  model: {model_key}", text)
                    editor.fill(text)
                    page.get_by_role("button", name="Run Demonstration", exact=True).click()
                    page.get_by_role("dialog").wait_for(state="hidden")
                    if wait:
                        wait_for_idle()
                assert page.get_by_role("combobox", name="Model", exact=True).count() == 0
                assert page.get_by_role("spinbutton").count() == 0
                change_preference(75, wait=False)
                page.get_by_text("Calculating plan…", exact=True).wait_for()
                assert settings.is_disabled()
                wait_for_idle()
                page.get_by_test_id("plan-action").get_by_text(re.compile(r"^Next 15 mins - As of .*11:05$")).wait_for()
                demonstration = page.get_by_text("[Demonstration - not part of actual plan, save config to disk to apply change]", exact=True)
                demonstration.wait_for()
                assert demonstration.evaluate("element => getComputedStyle(element).color") == "rgb(185, 28, 28)"
                page.get_by_test_id("plan-action").get_by_text("Self-consumption", exact=True).wait_for()
                assert page.get_by_test_id("plan-action").evaluate("element => getComputedStyle(element).backgroundColor") != "rgba(0, 0, 0, 0)"
                assert settings.is_enabled()
                settings.click()
                page.get_by_role("button", name="Run Demonstration", exact=True).click()
                page.get_by_text("Calculating plan…", exact=True).wait_for()
                wait_for_idle()
                assert settings.is_enabled()
                assert page.get_by_text(re.compile("^Model:")).count() == 0
                assert page.get_by_text("UI overrides active", exact=True).count() == 0
                assert page.get_by_text("No actual measurements available for today.", exact=True).count() == 0
                page.get_by_text("\u20ac7.500 (\u20ac7.50 to \u20ac40.00)", exact=True).wait_for()
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
                assert solar_buttons.get_by_role("button", name="Low (0.4x)", exact=True).evaluate("element => getComputedStyle(element).backgroundColor") == "rgb(245, 245, 245)"
                solar_buttons.get_by_role("button", name="Low (0.4x)", exact=True).click()
                page.get_by_text("10.00 / 12.50 kWh", exact=True).wait_for()
                solar_buttons.get_by_role("button", name="Med (1x)", exact=True).click()
                page.get_by_text("25.00 / 12.50 kWh", exact=True).wait_for()
                assert page.locator(".q-table tbody tr").count() == 7
                assert page.get_by_text("Records per page:", exact=True).count() == 0
                assert page.locator(".q-table tbody tr").last.locator("td").first.inner_text() == "Total"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(8).inner_text() == "25.00"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(9).inner_text() == "2.80"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(0).inner_text() == "11:05"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(1).inner_text() == "12:00"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(5).inner_text() == "0.4000"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(2).inner_text() == "0.92"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(2).inner_text() == "5.00"
                page.get_by_role("columnheader", name="SOC (%)", exact=True).wait_for()
                expected_headers = ["Start", "End", "hours", "Action", "Tariff", "Import rate", "Export rate",
                                    "House", "Solar", "Losses", "Import", "Export",
                                    "Battery", "SOC", "Cost", "Revenue", "Net"]
                headers = page.get_by_role("columnheader").all_text_contents()
                assert page.locator(".plan-detail-table th.sortable").count() == 0
                assert len(headers) == len(expected_headers)
                assert all(expected in actual for expected, actual in zip(expected_headers, headers))
                compact_count = page.get_by_role("columnheader").count()
                compact_end = page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).getOption().series[1].data.at(-1)[0]")
                page.get_by_role("button", name="Advanced", exact=True).click()
                page.get_by_role("columnheader", name="Battery end kWh", exact=True).wait_for()
                for label in ("Cost score", "Protection score", "Terminal score", "Total score"):
                    page.get_by_role("columnheader", name=label, exact=True).wait_for()
                page.get_by_text("Flow key:", exact=False).wait_for()
                advanced_headers = [header.replace("\n", " ").strip() for header in page.get_by_role("columnheader").all_text_contents()]
                action_text = page.locator(".q-table tbody tr").first.locator("td").nth(3).inner_text()
                assert action_text == "Self-consumption"
                loss_index = advanced_headers.index("Losses kWh")
                assert page.locator(".q-table tbody tr").last.locator("td").nth(loss_index).inner_text() == "2.46"
                assert advanced_headers[:5] == ["Start", "End", "hours", "Action", "Tariff"]
                assert advanced_headers.index("Load kWh") < advanced_headers.index("Solar kWh")
                assert not any("Battery start" in header or "Loss kWh" in header for header in advanced_headers)
                ordered_sections = ["S→B kWh", "G→B kWh", "B→G kWh", "Losses kWh", "Import kWh", "Export kWh",
                                    "Battery end kWh", "Cost score", "Protection score", "Terminal score", "Total score"]
                assert [advanced_headers.index(name) for name in ordered_sections] == sorted(advanced_headers.index(name) for name in ordered_sections)
                terminal_index = advanced_headers.index("Terminal score")
                assert page.locator(".q-table tbody tr").first.locator("td").nth(terminal_index).inner_text() == "0.00"
                assert page.locator(".q-table tbody tr").nth(-4).locator("td").nth(terminal_index).inner_text() == "0.50"
                assert page.locator(".q-table tbody tr").nth(-3).locator("td").first.inner_text() == "0–24h"
                assert page.locator(".q-table tbody tr").nth(-2).locator("td").first.inner_text() == "24–48h"
                assert page.locator(".q-table tbody tr").nth(-3).locator("td").nth(terminal_index).inner_text() == "0.00"
                assert page.locator(".q-table tbody tr").nth(-2).locator("td").nth(terminal_index).inner_text() == "0.50"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(terminal_index).inner_text() == "0.50"
                assert page.locator(".q-table tbody tr").first.locator("td").nth(2).inner_text() == "0.25"
                assert page.locator(".q-table tbody tr").count() == 29
                assert page.locator(".q-table tbody tr").nth(-3).locator("td").nth(2).inner_text() == "24.00"
                assert page.locator(".q-table tbody tr").nth(-2).locator("td").nth(2).inner_text() == "5.00"
                assert page.locator(".q-table tbody tr").last.locator("td").nth(2).inner_text() == "29.00"
                assert page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).getOption().series[1].data.at(-1)[0]") == compact_end + 24 * 3600 * 1000
                assert page.get_by_role("columnheader").count() > compact_count
                # Advanced headers and horizontal scrolling remain usable on desktop/mobile.
                def table_style():
                    return page.locator(".plan-detail-table").evaluate("""element => {
                        const properties = ['fontSize', 'lineHeight', 'paddingTop', 'paddingRight',
                            'paddingBottom', 'paddingLeft', 'marginTop', 'marginRight', 'marginBottom',
                            'marginLeft', 'minWidth', 'maxWidth', 'whiteSpace', 'borderSpacing'];
                        return ['th', 'td', 'th:nth-child(6)', 'td:nth-child(6)'].map(selector => {
                            const style = getComputedStyle(element.querySelector(selector));
                            return Object.fromEntries(properties.map(key => [key, style[key]]));
                        });
                    }""")

                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1100})
                    advanced_style = table_style()
                    assert advanced_style[0]["whiteSpace"] == "pre-line"
                    assert advanced_style[0]["maxWidth"] == "144px"
                    assert advanced_style[0]["minWidth"] == "0px"
                    assert advanced_style[0]["paddingLeft"] == "3px"
                    assert float(advanced_style[2]["minWidth"].removesuffix("px")) == pytest.approx(70.4, abs=.5)
                    assert float(advanced_style[2]["maxWidth"].removesuffix("px")) == pytest.approx(153.6, abs=.5)
                    assert float(advanced_style[2]["paddingLeft"].removesuffix("px")) == pytest.approx(3.2, abs=.1)
                    page.screenshot(path=str(tmp_path / f"plan-advanced-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Compact", exact=True).click()
                    page.wait_for_function("!Array.from(document.querySelectorAll('th')).some(e => e.textContent.includes('S→B'))")
                    assert table_style()[0]["maxWidth"] == advanced_style[0]["maxWidth"]
                    page.get_by_role("button", name="Advanced", exact=True).click()
                    page.get_by_role("columnheader", name="S→B kWh", exact=True).wait_for()
                    assert table_style() == advanced_style
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                page.set_viewport_size({"width": 1440, "height": 1100})
                page.get_by_role("button", name="Compact", exact=True).click()
                page.wait_for_function("!Array.from(document.querySelectorAll('th')).some(e => e.textContent.includes('S→B'))")
                assert page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).getOption().series[1].data.at(-1)[0]") == compact_end
                with page.expect_download() as download_info:
                    page.get_by_role("button", name="Download Excel", exact=True).click()
                download = download_info.value
                assert download.suggested_filename.startswith("plan-MILP-com-protection-75-solar-med-load-med-")
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
                change_preference(75, model_key="dynamic_programming")
                assert page.get_by_text(re.compile("^Model:")).count() == 0
                page.get_by_role("cell", name="DP", exact=True).first.wait_for()
                assert settings.is_enabled()
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
                load_buttons.get_by_role("button", name="Med (1x)", exact=True).click()
                solar_buttons.get_by_role("button", name="Med (1x)", exact=True).click()
                page.wait_for_function("echarts.getInstanceByDom(document.querySelector('[data-testid=plan-chart]')).getOption().series[1].data[0][1] === 2")
                assert page.get_by_test_id("plan-chart").evaluate("element => {const o = echarts.getInstanceByDom(element).getOption(); return o.legend[0].selected.Solar === false && o.dataZoom[0].start === 20 && o.dataZoom[0].end === 80;}")
                chart_id = page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).id")
                load_buttons.get_by_role("button", name="Low (0.5x)", exact=True).click()
                solar_buttons.get_by_role("button", name="High (1.5x)", exact=True).click()
                change_preference(25)
                page.get_by_text("\u20ac30.000 (-\u20ac2.50 to \u20ac30.00)", exact=True).wait_for()
                page.get_by_text("37.50 / 6.25 kWh", exact=True).wait_for()
                assert page.get_by_test_id("plan-chart").evaluate("element => echarts.getInstanceByDom(element).id") == chart_id
                assert page.get_by_test_id("plan-chart").evaluate("element => {const o = echarts.getInstanceByDom(element).getOption(); return o.legend[0].selected.Solar === false && o.dataZoom[0].start === 20 && o.dataZoom[0].end === 80;}")
                assert page.locator(".q-table tbody tr").count() == 7
                change_preference(99, model_key="mix_integer_linear_prog")
                page.get_by_text("Plan refresh failed. The previous plan is still shown.", exact=True).wait_for()
                assert settings.is_visible() and not settings.is_disabled()
                assert page.get_by_text(re.compile("^Model:")).count() == 0
                assert not settings.is_disabled()
                assert page.get_by_test_id("plan-chart").is_visible()
                page.get_by_role("button", name="Dismiss", exact=True).click()
                # The dialog validates drafts, saves only the temporary config, and loads global settings into the draft.
                settings.click()
                editor = page.get_by_role("textbox", name="YAML", exact=True)
                valid_text = editor.input_value()
                # Failed demonstrations retain the displayed report and its settings.
                assert "protection_preference: 25" in valid_text
                editor.fill("unknown_setting: 12")
                page.get_by_role("button", name="Run Demonstration", exact=True).click()
                page.get_by_text(re.compile("invalid configuration in energy_plan editor")).wait_for()
                assert page.get_by_role("dialog").is_visible()
                editor.fill(valid_text.replace("protection_preference: 25", "protection_preference: 123"))
                page.set_viewport_size({"width": 1440, "height": 1100})
                save = page.get_by_role("button", name="Save To Disk", exact=True)
                apply_button = page.get_by_role("button", name="Run Demonstration", exact=True)
                assert save.evaluate("element => element.classList.contains('q-btn--outline')")
                assert save.bounding_box()["x"] < apply_button.bounding_box()["x"]
                assert abs(save.bounding_box()["y"] - apply_button.bounding_box()["y"]) < 2
                save.click()
                page.get_by_text("Save energy plan settings to disk?", exact=True).wait_for()
                page.get_by_role("dialog").last.get_by_role("button", name="Cancel", exact=True).click()
                import yaml
                assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["energy_plan"]["protection_preference"] == 50
                assert editor.is_visible()
                save.click()
                page.get_by_role("button", name="Confirm save", exact=True).click()
                page.get_by_text("Energy plan settings saved to disk.", exact=True).wait_for()
                assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["energy_plan"]["protection_preference"] == 123
                assert editor.is_visible()
                page.get_by_role("button", name="Dismiss", exact=True).click()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1100})
                    page.wait_for_timeout(150)
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                    box = page.get_by_role("dialog").bounding_box()
                    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
                page.get_by_role("button", name="Load Global", exact=True).click()
                assert page.get_by_role("dialog").is_visible()
                playwright.expect(editor).to_have_value(re.compile(r"protection_preference: 50"))
                page.get_by_role("button", name="Cancel", exact=True).click()
                settings.click()
                assert "protection_preference: 25" in page.get_by_role("textbox", name="YAML", exact=True).input_value()
                page.get_by_role("button", name="Cancel", exact=True).click()
                # A second browser page has no overlay; saving never mutated the running global config.
                other = browser.new_page()
                other.goto(url)
                other.get_by_role("button", name="Plan settings", exact=True).click()
                assert "protection_preference: 50" in other.get_by_role("textbox", name="YAML", exact=True).input_value()
                other.close()
                assert not errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
