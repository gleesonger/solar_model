"""One Plan screen reads headless publications and pins historical snapshots."""
from pathlib import Path
import re
import socket
import sys
import time
from datetime import datetime, timedelta
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import pytest
from solar_model.runtime.processes import OwnedProcess

playwright = pytest.importorskip("playwright.sync_api")


def test_integrated_plan_history_and_headless_publication(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_path = tmp_path / "config.yaml"
    text = (repo / "config.yaml").read_text(encoding="utf-8").replace("path: solar.db", f"path: {tmp_path.as_posix()}/solar.db").replace("plan_rerun_frequency_mins: 5", "plan_rerun_frequency_mins: 0.02")
    config_path.write_text(text, encoding="utf-8")
    script = tmp_path / "app.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo)!r})
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from nicegui import ui, app, background_tasks
from solar_model.config import load_config
from solar_model.dashboard.layout import render_dashboard_styles
from solar_model.dashboard.tabs.plan import PlanTab
from solar_model.dashboard.plan_store import PlanStore
from solar_model.dashboard.planning_worker import stop_demonstrations
from solar_model.services.plan_history import persist_report
from solar_model.storage.migrations import open_database
from solar_model.storage.plan_history import load_plan, encode
from solar_model.storage.connection import create_engine_for_database
import yaml
from tests.runtime.helpers import collect, serve_planner, report

if __name__ == '__main__':
    config = load_config({str(config_path)!r})
    open_database(config.database.path).close()
    old = datetime.now(timezone.utc) - timedelta(days=2)
    old = old.replace(hour=12, minute=0, second=0, microsecond=0)
    original = report(config, old)
    historical_id = persist_report(config, original, old, old)
    stored = load_plan(config.database.path, historical_id)
    archived = yaml.safe_load(stored.metadata['settings'])
    archived.pop('history')
    archived_text = '# Historical settings preserved exactly\\n' + yaml.safe_dump(archived, sort_keys=False)
    stored.metadata['settings'] = archived_text
    with create_engine_for_database(config.database.path).begin() as connection:
        connection.exec_driver_sql("UPDATE plan_history SET payload = ? WHERE snapshot_id = ? AND view = 'metadata'", (encode(stored.metadata), historical_id))
        for offset in (-15, 15, 30):
            connection.exec_driver_sql("INSERT INTO sigenstor (collected_at_utc, plant_pv_power_kw, plant_load_power_kw, plant_battery_soc_percent, plant_grid_power_kw) VALUES (?, 20000, 15000, 250000, -4000)", (int((old + timedelta(minutes=offset)).timestamp()),))
    later = datetime.now(timezone.utc) - timedelta(minutes=10)
    persist_report(config, report(config, later), later, later)
    context = get_context('spawn')
    stop = context.Event()
    collector = context.Process(target=collect, args=(config.database.path, stop))
    planner = context.Process(target=serve_planner, args=({str(config_path)!r}, stop))
    collector.start()
    planner.start()
    store = PlanStore(config, {str(config_path)!r})
    async def start():
        background_tasks.create(store.run_forever())
    app.on_startup(start)
    app.on_shutdown(store.stop)
    app.on_shutdown(stop_demonstrations)
    def shutdown():
        stop.set()
        collector.join(3)
        planner.join(3)
    app.on_shutdown(shutdown)
    @app.get('/test/state')
    def state():
        return dict(version=store.version, latest=store.current.snapshot_id if store.current else None, historical=historical_id, archived_text=archived_text, collector=collector.pid, planner=planner.pid)
    @ui.page('/')
    def page():
        client = ui.context.client
        render_dashboard_styles()
        with ui.column().classes('w-full max-w-7xl mx-auto p-2 md:p-4'):
            with ui.tabs().classes('solar-tabs w-full') as tabs:
                for name in ['Recent','Analysis','Plan','System Info','Control']:
                    ui.tab(name)
            with ui.tab_panels(tabs, value='Plan').classes('w-full'):
                with ui.tab_panel('Plan').classes('min-w-0'):
                    tab = PlanTab(config, {str(config_path)!r}, store)
                    tab.render_plan_tab()
        unsubscribe = store.subscribe(lambda: client.safe_invoke(tab.apply_shared_plan))
        client.on_disconnect(unsubscribe)
    ui.run(host='127.0.0.1', port={port}, show=False, reload=False)
''', encoding="utf-8")
    owned = OwnedProcess([sys.executable, str(script)])
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert owned.poll() is None
            try:
                urlopen(url, timeout=1).close()
                break
            except Exception:
                time.sleep(.2)
        else:
            pytest.fail("Dashboard did not start")
        with playwright.sync_playwright() as driver:
            browser = None
            for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
                try:
                    browser = driver.chromium.launch(headless=True, **options)
                    break
                except playwright.Error:
                    continue
            if browser is None:
                pytest.skip("No Chromium browser installed")
            try:
                for width in (1440, 390):
                    page = browser.new_page(viewport={"width": width, "height": 1000}, accept_downloads=True)
                    page.set_default_timeout(15000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.get_by_test_id("plan-chart").wait_for()
                    assert page.get_by_role("tab", name="Plan History", exact=True).count() == 0
                    assert page.get_by_role("tab", name="Plan", exact=True).count() == 1
                    assert page.get_by_label("Plan date", exact=True).input_value() == ""
                    if width == 1440:
                        settings = page.get_by_role("button", name="Plan settings", exact=True).bounding_box()
                        date_box = page.get_by_label("Plan date", exact=True).bounding_box()
                        assert date_box["x"] > settings["x"] + settings["width"]
                    state = page.request.get(url + '/test/state').json()
                    page.get_by_test_id("plan-chart").evaluate("e => echarts.getInstanceByDom(e).dispatchAction({type:'legendUnSelect',name:'Grid Export'})")
                    old_date = (datetime.now(ZoneInfo('Europe/Dublin')) - timedelta(days=2)).date().isoformat()
                    page.get_by_label("Plan date", exact=True).fill(old_date)
                    snapshot = page.get_by_role("combobox", name="Snapshot", exact=True)
                    snapshot.click()
                    page.get_by_role("option", name="12:00:00 GMT", exact=True).click()
                    page.get_by_test_id("plan-action").get_by_text("12:00", exact=False).wait_for()
                    assert page.get_by_test_id("plan-chart").evaluate("e => echarts.getInstanceByDom(e).getOption().legend[0].selected['Grid Export']") is False
                    caption = page.get_by_test_id("plan-action").inner_text()
                    comparison = page.get_by_test_id("plan-chart").evaluate("""e => {
                        const o = echarts.getInstanceByDom(e).getOption();
                        const pair = o.series.filter(s => s.name === 'House Load');
                        return {legend: o.legend[0].data, count: pair.length, styles: pair.map(s => s.lineStyle.type),
                            connectActuals: pair[0].connectNulls, connectForecast: pair[1].connectNulls,
                            overlap: pair[0].data.some(a => a[1] !== null && pair[1].data.some(f => f[0] === a[0]))};
                    }""")
                    assert len(comparison['legend']) == 5 and comparison['count'] == 2
                    assert comparison['styles'] == ['solid', 'dashed'] and comparison['overlap']
                    assert comparison['connectActuals'] and not comparison['connectForecast']
                    assert page.get_by_test_id("plan-chart").evaluate("""e => {
                        const c = echarts.getInstanceByDom(e);
                        c.dispatchAction({type:'legendUnSelect',name:'House Load'});
                        const m = c.getModel();
                        return m.getSeriesByName('House Load').every(s => m.isSeriesFiltered(s));
                    }""")
                    page.get_by_test_id("plan-chart").evaluate("e => echarts.getInstanceByDom(e).dispatchAction({type:'legendSelect',name:'House Load'})")
                    chart_type = page.get_by_test_id("plan-forecast-chart-type")
                    chart_type.get_by_role("button", name="Bar", exact=True).click()
                    page.wait_for_function("Array.from(document.querySelectorAll('[data-testid=plan-chart]')).some(e => echarts.getInstanceByDom(e).getOption().series.every((s,i) => s.type === (s.name === 'Battery' ? 'line' : 'bar')))")
                    assert page.get_by_test_id("plan-chart").evaluate("e => echarts.getInstanceByDom(e).getOption().legend[0].selected['Grid Export']") is False
                    chart_box = page.get_by_test_id("plan-chart").bounding_box()
                    toggle_box = chart_type.bounding_box()
                    assert abs(toggle_box['y'] - chart_box['y']) < 10
                    assert abs(toggle_box['x'] + toggle_box['width'] - chart_box['x'] - chart_box['width']) < 2
                    if width == 390:
                        legend_right = page.get_by_test_id("plan-chart").evaluate("""e => {
                            const c = echarts.getInstanceByDom(e);
                            const v = c.getViewOfComponentModel(c.getModel().getComponent('legend'));
                            const g = v._controllerGroup, r = g.getBoundingRect();
                            return g.transformCoordToGlobal(r.x + r.width, r.y)[0];
                        }""")
                        assert chart_box['x'] + legend_right < toggle_box['x']
                    page.screenshot(path=str(tmp_path / f'plan-bars-{width}.png'), full_page=True)
                    page.get_by_role("button", name="Advanced", exact=True).click()
                    page.get_by_role("cell", name="0\u201324h", exact=True).wait_for()
                    assert page.get_by_test_id("plan-chart").evaluate("e => echarts.getInstanceByDom(e).getOption().series.every(s => s.type === (s.name === 'Battery' ? 'line' : 'bar'))")
                    page.get_by_role("button", name="Compact", exact=True).click()
                    chart_type.get_by_role("button", name="Line", exact=True).click()
                    page.wait_for_function("Array.from(document.querySelectorAll('[data-testid=plan-chart]')).some(e => echarts.getInstanceByDom(e).getOption().series.every(s => s.type === 'line'))")
                    page.get_by_role("button", name="Plan settings", exact=True).click()
                    assert page.get_by_label("YAML", exact=True).input_value() == state['archived_text']
                    page.get_by_role("button", name="Run Demonstration", exact=True).click()
                    page.get_by_text("missing required configuration value: energy_plan.history", exact=False).wait_for()
                    assert page.get_by_label("YAML", exact=True).input_value() == state['archived_text']
                    page.get_by_role("button", name="Save To Disk", exact=True).click()
                    page.get_by_text("missing required configuration value: energy_plan.history", exact=False).wait_for()
                    page.get_by_role("button", name="Load Global", exact=True).click()
                    playwright.expect(page.get_by_label("YAML", exact=True)).to_have_value(re.compile(r"[\s\S]*history:[\s\S]*"))
                    page.get_by_role("button", name="Cancel", exact=True).click()
                    # The service continues producing while this browser is pinned.
                    page.wait_for_function("async v => (await (await fetch('/test/state')).json()).version > v", arg=state['version'])
                    assert page.get_by_test_id("plan-action").inner_text() == caption
                    with page.expect_download() as download:
                        page.get_by_role("button", name="Download Excel", exact=True).click()
                    assert download.value.suggested_filename.endswith('.xlsx')
                    page.get_by_label("Plan date", exact=True).fill("")
                    page.get_by_test_id("plan-action").get_by_text("12:00", exact=False).wait_for(state="hidden")
                    page.get_by_role("button", name="Advanced", exact=True).click()
                    page.get_by_role("cell", name="0\u201324h", exact=True).wait_for()
                    page.get_by_text("Waiting for the planning service", exact=False).wait_for(state="hidden")
                    page.screenshot(path=str(tmp_path / f'plan-{width}.png'), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0,12).map(e => [e.tagName,e.className,e.getBoundingClientRect().width])")
                    assert errors == []
                    page.close()
            finally:
                browser.close()
    finally:
        owned.stop()
