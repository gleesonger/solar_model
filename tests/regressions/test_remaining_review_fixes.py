"""Independent expected-value regressions for review R05-R19; temporary DBs only."""
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from solar_model.time_utils import add_elapsed, utc_instant
from solar_model.config import SolarArrayConfig, TariffPeriod, TariffRule, TariffsConfig, load_config
import solar_model.storage.queries.dashboard as data
from solar_model.storage.schema import HOURLY_ROLLUP_MARKER, ForecastSolarSample, SigenStorHourlySample, SigenStorModbusSample
from solar_model.storage.connection import SolarDatabase
from solar_model.storage.migrations import open_database
from solar_model.planning.types import DeviceInfo, DeviceState, ForecastData, ForecastInterval, ScenarioData
from solar_model.planning.optimisation import simulate_energy_plan
from solar_model.tools.evaluate_forecasts import forecast_performance
from solar_model.planning.forecasts.solar import _fit_scalars, _official_day_frame, _training_frames, add_adjusted_energy_values, calculate_adjusted_forecast
from solar_model.storage.queries.forecasting import load_operational_inputs
from solar_model.planning.scenarios import create_scenarios
from solar_model.tools.reports import group_plan
from solar_model.storage.queries.planning import interval_energies

UTC = timezone.utc
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def rule(rate):
    return TariffRule(DAYS, "00:00", "24:00", rate, "fixed")


def tariffs(rate=.4):
    return TariffsConfig((TariffPeriod("2020-01-01", (rule(rate),), (rule(.1),)),))


@pytest.fixture
def database(tmp_path):
    db = open_database(tmp_path / "fixture.db")
    yield db
    db.close()


@pytest.mark.parametrize("fail", [False, True])
def test_tariff_raw_and_rollup_commit_or_rollback_together(database, monkeypatch, fail):
    import solar_model.tools.recalculate_economics as recalc
    with database.sessions.begin() as session:
        session.add(SigenStorModbusSample(collected_at_utc="2026-01-02T10:00Z", import_tariff_band="fixed",
                                         import_rate=.1, grid_import_cost_period=.1, plant_grid_import_total_kwh_period=1.))
    database.rebuild_hourly_rollup(date(2026, 1, 2), date(2026, 1, 2), ZoneInfo("UTC"))
    cfg = load_config()
    monkeypatch.setattr(recalc, "load_config", lambda _: replace(cfg, timezone="UTC", tariffs=tariffs(.5),
                       database=replace(cfg.database, path=database.engine.url.database)))
    if fail:
        original = SolarDatabase._insert_hourly_rollup

        def fail_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("injected rebuild failure")

        monkeypatch.setattr(SolarDatabase, "_insert_hourly_rollup", staticmethod(fail_after_insert))
        with pytest.raises(SystemExit) as error:
            recalc.main(["--from", "2026-01-02", "--to", "2026-01-02"])
        assert error.value.code == 1
    else:
        assert recalc.main(["--from", "2026-01-02"]) == 0
    with database.sessions() as session:
        for model in (SigenStorModbusSample, SigenStorHourlySample):
            row = session.scalars(select(model)).one()
            assert row.import_rate == (.1 if fail else .5)
            assert row.grid_import_cost_period == (.1 if fail else .5)


@pytest.mark.parametrize("values,expected", [([4., None], 4.), ([4., 0.], 2.), ([None, None], None)])
def test_nullable_means_count_each_field_and_match_rebuild(database, values, expected):
    previous = {}
    for day in (1, 2):
        for minute, value in enumerate(values):
            readings = {} if value is None else {"plant_pv_power": (value, "kW", False),
                                                 "plant_battery_soc": (value * 10, "%", False)}
            stamp = f"2026-01-{day:02d}T10:{minute:02d}:00+00:00"
            database.save_modbus_sample(readings, previous, (stamp, stamp), tariffs())
    bounds = data.analysis_date_bounds(date(2026, 1, 1), date(2026, 1, 2), UTC)
    outputs = []
    for resolution in ("minute", "hour"):
        output = data.load_chart_data(database.engine.url.database, "UTC", bounds, "hour", "average", (), {},
                                      (("power_solar", None),), sample_resolution=resolution)
        value = output["series"][0]["data"][output["periods"].index("10:00")]
        assert value is None if expected is None else value == pytest.approx(expected)
        outputs.append(output)
    with database.sessions() as session:
        rows = session.scalars(select(SigenStorHourlySample)).all()
        assert sum(row.plant_pv_power_kw_sample_count for row in rows) == 2 * sum(v is not None for v in values)
        _, battery = data.average_hourly_rollup_telemetry(rows, ZoneInfo("UTC"), {})
        soc = battery.loc[battery.time == "10:00", "soc_percent"].iloc[0]
        assert pd.isna(soc) if expected is None else soc == pytest.approx(expected * 10)
    database.rebuild_hourly_rollup(date(2026, 1, 1), date(2026, 1, 2), ZoneInfo("UTC"))
    assert data.load_chart_data(database.engine.url.database, "UTC", bounds, "hour", "average", (), {},
                                (("power_solar", None),)) == outputs[-1]


@pytest.mark.parametrize("interrupt", [False, True])
def test_v2_rollups_receive_atomic_v3_valid_counts(database, monkeypatch, interrupt):
    with database.sessions.begin() as session:
        session.add_all([SigenStorModbusSample(collected_at_utc="2026-01-02T10:00Z", plant_pv_power_kw=4.),
                         SigenStorModbusSample(collected_at_utc="2026-01-02T10:01Z")])
    database.rebuild_hourly_rollup(date(2026, 1, 2), date(2026, 1, 2), ZoneInfo("UTC"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM database_metadata WHERE key=?", (HOURLY_ROLLUP_MARKER,))
        connection.exec_driver_sql("INSERT OR REPLACE INTO database_metadata VALUES ('hourly_rollup_initialized_v2', '1')")
        connection.exec_driver_sql("UPDATE sigenstor_hourly SET plant_pv_power_kw_sample_count=NULL")
    if interrupt:
        original = SolarDatabase._insert_hourly_rollup
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("interrupted v3 upgrade")
            patch.setattr(SolarDatabase, "_insert_hourly_rollup", staticmethod(fail))
            with pytest.raises(RuntimeError):
                open_database(database.engine.url.database)
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM database_metadata WHERE key=?", (HOURLY_ROLLUP_MARKER,)).scalar() == 0
            assert connection.exec_driver_sql("SELECT plant_pv_power_kw_sample_count FROM sigenstor_hourly").scalar() is None
    upgraded = open_database(database.engine.url.database)
    try:
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT plant_pv_power_kw_sample_count FROM sigenstor_hourly").scalar() == 1
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM sigenstor").scalar() == 2
    finally:
        upgraded.close()


@pytest.mark.parametrize("zone_name", ["Asia/Kathmandu", "Asia/Kolkata", "Europe/Dublin"])
def test_local_rebuild_preserves_both_sides_of_overlapping_utc_hours(database, zone_name):
    zone = ZoneInfo(zone_name)
    start = datetime(2026, 1, 2, tzinfo=zone)
    end = datetime(2026, 1, 3, tzinfo=zone)
    # Offset zones straddle UTC hours. Dublin is aligned, so adjacent hours stay intact.
    stamps = [utc_instant(boundary) + timedelta(minutes=minutes) for boundary in (start, end) for minutes in (-10, 10)]
    with database.sessions.begin() as session:
        for stamp in stamps:
            session.add(SigenStorModbusSample(collected_at_utc=stamp.isoformat(), plant_pv_power_kw=1.))
    database.rebuild_hourly_rollup(date(2026, 1, 1), date(2026, 1, 4), zone)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE sigenstor SET plant_pv_power_kw=20000 WHERE collected_at_utc>=? AND collected_at_utc<?",
                                   (int(start.timestamp()), int(end.timestamp())))
    for _ in range(2):
        database.rebuild_hourly_rollup(start.date(), start.date(), zone)
        with database.engine.connect() as connection:
            rows = connection.exec_driver_sql("SELECT sample_count, plant_pv_power_kw FROM sigenstor_hourly ORDER BY hour_start_utc").all()
            assert sum(r[0] for r in rows) == 4
            assert sum(r[1] for r in rows) == 60000
            if zone_name != "Europe/Dublin":
                assert rows == [(2, 30000), (2, 30000)]


@pytest.mark.parametrize("timestep", ["minute", "hour", "day", "week", "month", "season", "year"])
@pytest.mark.parametrize("forecasts", [False, True])
def test_download_and_telemetry_exclude_next_midnight(database, timestep, forecasts):
    zone = ZoneInfo("Europe/Dublin")
    with database.sessions.begin() as session:
        for stamp, value in [("2026-10-14T23:59:59+01:00", 1.), ("2026-10-15T00:00:00+01:00", 10.)]:
            session.add(SigenStorModbusSample(collected_at_utc=stamp, plant_load_total_kwh_period=value, plant_pv_power_kw=value))
    bounds = data.analysis_date_bounds(date(2026, 10, 14), date(2026, 10, 14), zone)
    frame = data.analysis_export_dataframe(database.engine.url.database, bounds, timestep, zone, include_forecast=forecasts)
    assert frame["Load (kWh)"].sum() == 1.
    result = data.load_chart_data(database.engine.url.database, zone.key, bounds, "day", "average", (), {},
                                 (("power_solar", None),), sample_resolution="minute")
    assert result["periods"] == ["2026-10-14"]
    assert result["series"][0]["data"] == [1.]


def test_configured_local_collection_date_and_panel_two_vintages(database):
    zone = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 9, 10, tzinfo=zone)
    panel = SolarArrayConfig(2, "Two", 53., -6., 30., 0., 3.)
    with database.sessions.begin() as session:
        for minute, value in [(-60, 1000.), (15, 2000.), (60, 3000.)]:
            session.add(ForecastSolarSample(collected_at_utc=(start + timedelta(minutes=minute)).isoformat(),
                        forecast_time=(start + timedelta(hours=12)).isoformat(), panel_2_adj_watts=value, panel_2_adj_watt_hours=value))
        # A newer unconfigured panel and a next-day target cannot displace panel 2.
        session.add(ForecastSolarSample(collected_at_utc=(start + timedelta(hours=2)).isoformat(),
                    forecast_time=(start + timedelta(hours=12)).isoformat(), panel_1_adj_watts=9000.))
        session.add(ForecastSolarSample(collected_at_utc=(start + timedelta(hours=3)).isoformat(),
                    forecast_time=(start + timedelta(days=1, hours=12)).isoformat(),
                    panel_2_adj_watts=9000., panel_2_adj_watt_hours=9000.))
    with database.sessions() as session:
        latest = data.load_latest_adjusted_forecast_for_day(session, start, (2,))
        bod = data.load_bod_adjusted_forecast_for_day(session, start, (2,))
        assert pd.Timestamp(latest[0].collected_at_utc) == pd.Timestamp(start + timedelta(hours=1))
        assert pd.Timestamp(bod[0].collected_at_utc) == pd.Timestamp(start + timedelta(minutes=15))
    daily = data.load_daily_energy_totals(database.engine.url.database, zone.key, start.date(), start.date(), (panel,))
    assert daily.iloc[0].solar_forecast == 2.
    # Earliest pre-midnight collection genuinely belongs to the previous local day.
    assert data.load_lifetime_start_date(database.engine.url.database, start.date(), zone.key) == date(2026, 9, 9)


def forecast_frame(snapshot, targets, values):
    return pd.DataFrame([dict(snapshot=pd.Timestamp(snapshot), target=pd.Timestamp(t), target_hour=pd.Timestamp(t).floor("h"),
                             raw_total_kw=v, raw_1_kw=v, raw_2_kw=0., raw_3_kw=0., raw_4_kw=0.) for t, v in zip(targets, values)])


def empty_actuals():
    return pd.DataFrame({"target_hour": pd.Series([], dtype="datetime64[ns, UTC]"), "plant_pv_power_kw": pd.Series([], dtype=float)})


def test_exact_forecast_targets_energy_and_differing_grid_interpolation():
    source = forecast_frame("2026-01-03T23:00Z", ["2026-01-04T10:00Z", "2026-01-04T10:30Z", "2026-01-04T11:00Z"], [1., 2., 3.])
    snapshot, values = calculate_adjusted_forecast(source, empty_actuals(), "2026-01-04T09:00Z", "UTC")
    assert [v["panel_1_adj_watts"] for v in values.values()] == [1000., 2000., 3000.]
    energy = add_adjusted_energy_values(source, snapshot, date(2026, 1, 4), ZoneInfo("UTC"), values)
    assert next(iter(energy.values()))["panel_1_adj_watt_hours"] == 500.
    dest = forecast_frame("2026-01-04T08:00Z", ["2026-01-04T09:30Z", "2026-01-04T10:15Z", "2026-01-04T11:30Z"], [99., 99., 99.])
    snapshot, values = calculate_adjusted_forecast(pd.concat([source, dest]), empty_actuals(), "2026-01-04T09:00Z", "UTC")
    assert snapshot == pd.Timestamp("2026-01-04T08:00Z")
    assert values == {"2026-01-04T10:15:00+00:00": {f"panel_{p}_adj_watts": 1500. if p == 1 else 0. for p in (1, 2, 3, 4)}}


@pytest.mark.parametrize("day,end_hour", [(date(2026, 3, 29), 5), (date(2026, 10, 25), 3)])
def test_dst_overlap_scenarios_and_solver_use_real_elapsed_hours(day, end_hour):
    zone = ZoneInfo("Europe/Dublin")
    start = datetime.combine(day, datetime.min.time(), zone)
    end = start.replace(hour=end_hour)
    interval = ForecastInterval(start, end, 4.)
    tail = ForecastInterval(utc_instant(end), utc_instant(end) + timedelta(hours=24), 0.)
    forecast = ForecastData([interval, tail], [interval, tail])
    for hour in range(4):
        assert forecast.sum(add_elapsed(start, timedelta(hours=hour)), add_elapsed(start, timedelta(hours=hour + 1))).solar_kwh == 1.
    scenarios = create_scenarios(forecast)
    assert next(scenario.forecast_data for scenario in scenarios if scenario.name == "base").sum(start, end).solar_kwh == 4.
    cfg = load_config()
    cfg = replace(cfg, tariffs=tariffs(), energy_plan=replace(cfg.energy_plan, num_projection_hours=4,
                  projection_resolution_mins=(60,) * 24, minimum_battery_kwh=(rule(0.),), maximum_battery_kwh=(rule(10.),)))
    device = DeviceInfo(**{f.name: 1. for f in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=10., battery_state_of_health_percent=100.,
                     inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
                     inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.)
    plan = simulate_energy_plan(device, DeviceState(2.), [ScenarioData("base", forecast, 1.)], cfg, start).results["base"]
    assert plan.solar_kwh.tolist() == [1.] * 4
    assert [(utc_instant(row.ends_at) - utc_instant(row.starts_at)).total_seconds()
            for row in plan.itertuples()] == [3600] * 4
    assert plan.battery_end_kwh.between(-1e-7, 10 + 1e-7).all()
    grouped = group_plan(plan, 60)
    assert len(grouped) == 4
    assert grouped.solar_kwh.sum() == 4.


def test_fold_targets_do_not_collapse_in_cumulative_forecast_conversion():
    rows = [ForecastSolarSample(forecast_time=stamp, panel_1_adj_watt_hours=wh) for stamp, wh in
            [("2026-10-25T01:00:00+01:00", 1000.), ("2026-10-25T01:00:00+00:00", 2000.)]]
    values = interval_energies(rows, (1,), ZoneInfo("Europe/Dublin"), "adj")
    assert len(values) == 2
    assert list(values.values()) == [1., 1.]


def test_evaluation_excludes_open_hour_and_is_frozen_by_as_of(database):
    with database.sessions.begin() as session:
        for hour in (9, 10, 11):
            session.add(SigenStorModbusSample(collected_at_utc=f"2026-01-02T{hour:02d}:05Z", plant_pv_power_kw=2.))
            session.add(ForecastSolarSample(collected_at_utc="2026-01-02T08:00Z", forecast_time=f"2026-01-02T{hour:02d}:00Z",
                                           panel_1_raw_watts=2000., panel_1_adj_watts=2000.))
    before = forecast_performance(database.engine.url.database, as_of="2026-01-02T10:15Z")
    assert before.target.tolist() == [pd.Timestamp("2026-01-02T09:00Z")]
    with database.sessions.begin() as session:
        session.add(SigenStorModbusSample(collected_at_utc="2026-01-02T10:45Z", plant_pv_power_kw=99.))
    pd.testing.assert_frame_equal(before, forecast_performance(database.engine.url.database, as_of="2026-01-02T10:15Z"))


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_database_planning_loader_covers_four_elapsed_dst_hours(database, monkeypatch, day):
    import solar_model.storage.queries.planning as planner
    start = datetime.combine(day, datetime.min.time(), ZoneInfo("Europe/Dublin"))
    cfg = load_config()
    cfg = replace(cfg, timezone="Europe/Dublin", forecast=replace(cfg.forecast, solar=replace(cfg.forecast.solar, arrays=(cfg.forecast.solar.arrays[0],))),
                  energy_plan=replace(cfg.energy_plan, num_projection_hours=4))
    panel = cfg.forecast.solar.arrays[0].panel_id
    with database.sessions.begin() as session:
        for hour in range(1, 5):
            session.add(ForecastSolarSample(collected_at_utc=start.isoformat(),
                        forecast_time=add_elapsed(start, timedelta(hours=hour)).isoformat(),
                        **{f"panel_{panel}_adj_watt_hours": hour * 1000.}))
    with database.sessions.begin() as session:
        for stamp in pd.date_range(pd.Timestamp(start) - pd.Timedelta(days=2), pd.Timestamp(start), freq="h", inclusive="left"):
            session.add(SigenStorHourlySample(hour_start_utc=stamp.isoformat(), import_tariff_band="day",
                        sample_count=1, house_load_kwh_period=1., plant_load_total_kwh_period=2.))
    forecast = planner.load_forecast_as_of(database, start, cfg)
    assert len(forecast.load) == 28
    point = forecast.sum(start, add_elapsed(start, timedelta(hours=4)))
    assert (point.solar_kwh, point.load_kwh) == (4., 4.)


def test_nullable_rates_and_levels_use_their_own_valid_counts(database):
    with database.sessions.begin() as session:
        for minute, rate, level in [(0, .4, 8.), (1, None, None), (2, 0., 10.)]:
            session.add(SigenStorModbusSample(collected_at_utc=f"2026-01-02T10:{minute:02d}Z", import_rate=rate,
                                             plant_battery_available_discharge_kwh=level))
    database.rebuild_hourly_rollup(date(2026, 1, 2), date(2026, 1, 2), ZoneInfo("UTC"))
    with database.sessions() as session:
        row = session.scalars(select(SigenStorHourlySample)).one()
        assert row.sample_count == 3
        assert row.import_rate_sample_count == 2
        assert row.plant_battery_available_discharge_kwh_sample_count == 2
        _, battery = data.average_hourly_rollup_telemetry([row], ZoneInfo("UTC"), {})
        assert battery.loc[battery.time == "10:00", "available_energy_kwh"].iloc[0] == 9.
        hourly = data.actual_hourly_from_rollup_samples([row], datetime(2026, 1, 2, tzinfo=UTC), fill_missing=False)
        assert hourly.loc[hourly.hour == "10:00", "import_rate"].iloc[0] == .2


def test_scalar_training_does_not_fall_back_when_selected_vintage_lacks_targets():
    forecasts = pd.concat([
        forecast_frame("2026-01-01T23:00Z", ["2026-01-03T10:00Z"], [1.]),
        forecast_frame("2026-01-02T23:00Z", ["2026-01-04T10:00Z"], [2.]),
    ], ignore_index=True)
    assert _training_frames(forecasts, empty_actuals(), date(2026, 1, 4), ZoneInfo("UTC"), pd.Timestamp("2026-01-04T09:00Z")).empty


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("failure_position", [0, 1])
def test_supervisor_partial_launch_cleans_up_children(monkeypatch, restart, failure_position):
    import solar_model.runtime.supervisor as docker_start
    first, second, partial = [Mock() for _ in range(3)]
    for child in (first, second, partial):
        child.poll.return_value = None
    failure = OSError("launch failed")
    launches = ([first, second] if restart else []) + ([partial] if failure_position else []) + [failure]
    monkeypatch.setattr(docker_start.subprocess, "Popen", Mock(side_effect=launches))
    monkeypatch.setattr(docker_start, "open_database", Mock())
    monkeypatch.setattr(docker_start.signal, "signal", Mock())
    monkeypatch.setattr(docker_start.Path, "exists", lambda _: True)
    monkeypatch.setattr(docker_start.Path, "unlink", lambda *args, **kwargs: None)
    with pytest.raises(OSError, match="launch failed"):
        docker_start.main()
    if failure_position:
        partial.terminate.assert_called_once()
        partial.wait.assert_called_once_with(timeout=10)
    if restart:
        for child in (first, second):
            child.terminate.assert_called()
            child.wait.assert_called()


def test_model_sql_input_means_and_bounded_training_preserve_scalar_fit(database, monkeypatch):
    with database.sessions.begin() as session:
        session.add_all([SigenStorModbusSample(collected_at_utc="2026-01-02T10:00Z", plant_pv_power_kw=2.),
                         SigenStorModbusSample(collected_at_utc="2026-01-02T10:30Z", plant_pv_power_kw=4.),
                         SigenStorModbusSample(collected_at_utc="2026-01-02T10:45Z")])
    _, actuals = load_operational_inputs(database)
    assert actuals.plant_pv_power_kw.tolist() == [3.]
    frames = []
    hours = pd.date_range("2026-01-01", "2026-04-01T18:00", freq="6h", tz="UTC")
    for target in hours:
        frames.append(forecast_frame((target.floor("D") - pd.Timedelta(hours=1)).isoformat(), [target.isoformat()], [1.]))
    forecasts = pd.concat(frames, ignore_index=True)
    actuals = pd.DataFrame({"target_hour": hours, "plant_pv_power_kw": np.linspace(.5, 2., len(hours))})
    zone = ZoneInfo("Europe/Dublin")
    day = date(2026, 4, 1)
    as_of = pd.Timestamp("2026-04-01T10:15Z")
    old_frames = [_official_day_frame(forecasts, actuals, d.date(), zone)[1]
                  for d in pd.date_range("2026-01-01", "2026-03-31")]
    reference = pd.concat(old_frames, ignore_index=True)
    bounded = _training_frames(forecasts, actuals, day, zone, as_of)
    assert len(bounded) < len(reference)
    assert (bounded.target >= as_of - pd.Timedelta(days=56)).all()
    np.testing.assert_allclose(_fit_scalars(bounded, as_of), _fit_scalars(reference, as_of), rtol=1e-13)
    result = calculate_adjusted_forecast(forecasts, actuals, as_of, zone.key)
    assert result[1]
    with monkeypatch.context() as patch:
        patch.setattr("solar_model.planning.forecasts.solar._training_frames", lambda *args: reference)
        expected = calculate_adjusted_forecast(forecasts, actuals, as_of, zone.key)
    for target, values in result[1].items():
        assert values == pytest.approx(expected[1][target], rel=1e-13)
    future_actuals = pd.concat([actuals, pd.DataFrame({"target_hour": [pd.Timestamp("2026-04-03T12:00Z")],
                                                     "plant_pv_power_kw": [9999.]})], ignore_index=True)
    assert calculate_adjusted_forecast(forecasts, future_actuals, as_of, zone.key) == result


@pytest.mark.parametrize("hours", [1, 6, 23, 24, 25])
@pytest.mark.parametrize("anchor", [date(2026, 3, 29), date(2026, 10, 25)])
def test_hour_range_date_navigation_is_symmetric_and_day_based(hours, anchor):
    back = data.shift_analysis_date(anchor, hours, "hours", -1)
    assert back == anchor - timedelta(days=1)
    assert data.shift_analysis_date(back, hours, "hours", 1) == anchor
