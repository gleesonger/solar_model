from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from solar_model.config import load_config
from solar_model.dashboard.tabs.plan import detail_frame, detail_rows, detail_totals, hourly_detail_frame, plan_chart_options, scenario_summary
from solar_model.planning.types import NextAction, OperatingMode, SimulationResult


def sample_result():
    start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
    frame = pd.DataFrame({
        "starts_at": [start, start + timedelta(minutes=15)],
        "ends_at": [start + timedelta(minutes=15), start + timedelta(minutes=75)],
        "solar_kwh": [.5, 2.], "load_kwh": [.25, 1.],
        "battery_start_kwh": [4., 4.2], "battery_end_kwh": [4.2, 3.],
        "net_cost": [1., -.25], "grid_import_kwh": [.3, .8], "grid_export_kwh": [0., .1],
        "tariff_name": ["day", "day"],
        "import_rate": [.4, .4], "export_rate": [.2, .2],
        "solar_to_battery_loss_kwh": [.01, .02], "grid_to_battery_loss_kwh": [.02, .03],
        "battery_to_ac_loss_kwh": [.03, .04], "solar_clipped_kwh": [0., .1],
        "import_cost": [.12, .32], "export_revenue": [0., .02],
    })
    action = NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0., 100., 0.)
    return SimulationResult(action, {"base": frame, "low_high": frame.assign(net_cost=2.)})


def test_summary_and_chart_use_duration_weighting_and_consistent_units():
    from solar_model.planning.optimisation import battery_protection_score
    frame = sample_result().results["base"]
    summary = scenario_summary(frame, 4.)
    assert summary["cost"] == .75
    assert summary["solar"] == 2.5
    assert summary["load"] == 1.25
    assert summary["minimum"] == summary["ending"] == 3.
    assert summary["below_reserve"] == 1.
    assert summary["protection"] == pytest.approx((.25 * battery_protection_score(4.2, 4.) + battery_protection_score(3., 4.)) / 1.25)
    actuals = pd.DataFrame({"timestamp": [frame.starts_at.iloc[0]], "solar": [2.], "house_load": [1.], "battery": [4.], "grid_import": [.3], "grid_export": [0.]})
    options = plan_chart_options(frame, actuals, "Europe/Dublin")
    assert options["legend"]["data"] == ["Solar", "House Load", "Battery", "Grid Import", "Grid Export"]
    actual, forecast = options["series"][:2]
    assert actual["name"] == forecast["name"] == "Solar"
    assert actual["lineStyle"]["type"] == "solid"
    assert forecast["lineStyle"]["type"] == "dashed"
    assert [point[1] for point in forecast["data"]] == [2., 2., 2.]
    assert options["series"][4]["yAxisIndex"] == 1
    assert options["series"][5]["data"][0][1] == 4.
    assert options["series"][7]["data"][0][1] == pytest.approx(1.2)
    assert options["series"][7]["data"][1][1] == pytest.approx(.8)
    assert options["series"][9]["data"][1][1] == pytest.approx(.1)
    assert options["series"][7]["yAxisIndex"] == options["series"][9]["yAxisIndex"] == 0


def test_detail_rows_show_local_time_only_and_preserve_missing_values():
    frame = sample_result().results["base"].copy()
    frame["starts_at"] = pd.to_datetime(["2026-10-25T00:30Z", "2026-10-25T01:30Z"])
    frame.loc[0, "solar_kwh"] = float("nan")
    rows = detail_rows(frame, "Europe/Dublin")
    assert rows[0]["starts_at"] == "01:30"
    assert rows[1]["starts_at"] == "01:30"
    assert rows[0]["period"] != rows[1]["period"]
    assert rows[0]["solar_kwh"] is None
    assert rows[1]["solar_kwh"] == 2.
    assert frame.solar_kwh.isna().iloc[0]


def test_detail_totals_sum_flows_and_costs_without_summing_levels_or_rates():
    frame = sample_result().results["base"].assign(
        minimum_battery_kwh=4., maximum_battery_kwh=18., import_rate=.4,
        limit_battery_export_rate_to_grid_kw=5., solar_clipped_kwh=float("nan"),
    )
    totals = detail_totals(frame)
    assert totals["starts_at"] == "Total"
    assert totals["solar_kwh"] == 2.5
    assert totals["load_kwh"] == 1.25
    assert totals["net_cost"] == .75
    for column in ("ends_at", "battery_start_kwh", "battery_end_kwh", "minimum_battery_kwh",
                   "maximum_battery_kwh", "import_rate", "limit_battery_export_rate_to_grid_kw", "solar_clipped_kwh"):
        assert totals[column] is None


def test_detail_actions_use_shared_first_mode_and_later_grid_battery_flows():
    frame = sample_result().results["base"]
    frame = pd.concat([frame, frame.iloc[[1]]], ignore_index=True).assign(
        grid_to_battery_kwh=[0., .5, 0.], battery_to_grid_kwh=[0., 0., .4],
    )
    rows = detail_rows(frame, "Europe/Dublin", OperatingMode.DISCHARGE_PV_FIRST)
    assert [row["plan_action"] for row in rows] == ["Force discharge", "Force charge", "Force discharge"]
    frame["grid_to_battery_kwh"] = 1e-9
    frame["battery_to_grid_kwh"] = 0.
    assert all(row["plan_action"] == "Self-consumption" for row in detail_rows(frame, "Europe/Dublin"))
    assert "plan_action" not in frame.columns


def test_detail_soc_uses_nameplate_capacity_and_stays_beside_energy():
    original = sample_result().results["base"]
    frame = detail_frame(original, 20.)
    assert frame.battery_start_soc_percent.tolist() == pytest.approx([20., 21.])
    assert frame.battery_end_soc_percent.tolist() == pytest.approx([21., 15.])
    assert frame.columns.get_loc("battery_end_soc_percent") == frame.columns.get_loc("battery_end_kwh") + 1
    assert detail_totals(frame)["battery_end_soc_percent"] is None
    assert frame.losses_kwh.tolist() == pytest.approx([.06, .09])
    assert detail_totals(frame)["losses_kwh"] == .15
    assert detail_frame(original, None).battery_end_soc_percent.isna().all()
    assert "battery_end_soc_percent" not in original.columns


def test_compact_hours_split_flows_preserve_totals_and_interpolate_battery():
    frame = detail_frame(sample_result().results["base"], 20.)
    frame["import_rate"] = [.2, .5]
    frame["tariff_name"] = ["night", "day"]
    hourly = hourly_detail_frame(frame, "Europe/Dublin", OperatingMode.CHARGE_PV_FIRST)
    assert hourly.starts_at.dt.strftime("%H:%M").tolist() == ["10:05", "11:00"]
    assert hourly.ends_at.dt.strftime("%H:%M").tolist() == ["11:00", "11:20"]
    for name in ("solar_kwh", "grid_import_kwh", "net_cost", "losses_kwh"):
        assert hourly[name].sum() == pytest.approx(frame[name].sum())
    assert hourly.battery_end_kwh.tolist() == pytest.approx([3.4, 3.])
    assert hourly.battery_end_soc_percent.tolist() == pytest.approx([17., 15.])
    assert hourly.import_rate.iloc[0] == pytest.approx((.2 * 15 + .5 * 40) / 55)
    assert hourly.tariff_name.iloc[0] == "night / day"
    assert hourly.plan_action.iloc[0] == "Force charge / Self-consumption"
    assert detail_rows(hourly, "Europe/Dublin", OperatingMode.CHARGE_PV_FIRST)[0]["plan_action"] == hourly.plan_action.iloc[0]


def test_compact_hours_keep_repeated_local_hours_separate():
    frame = sample_result().results["base"].iloc[[0]].copy()
    frame["starts_at"] = pd.to_datetime(["2026-10-25T00:45Z"])
    frame["ends_at"] = pd.to_datetime(["2026-10-25T02:15Z"])
    hourly = hourly_detail_frame(detail_frame(frame, 20.), "Europe/Dublin", OperatingMode.SELF_CONSUMPTION)
    assert len(hourly) == 3
    assert hourly.starts_at.dt.tz_convert("Europe/Dublin").dt.hour.tolist() == [1, 1, 2]
    assert hourly.solar_kwh.sum() == pytest.approx(frame.solar_kwh.sum())


def test_actuals_use_physical_units_preserve_gaps_and_exclude_future(tmp_path):
    from solar_model.storage.migrations import open_database
    from solar_model.storage.schema import SigenStorModbusSample
    from solar_model.storage.queries.planning import load_plan_actuals
    db = open_database(tmp_path / "plan.db")
    start = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
    try:
        with db.sessions.begin() as session:
            for minute in (-1, 0, 2, 3):
                session.add(SigenStorModbusSample(collected_at_utc=start + timedelta(minutes=minute),
                    plant_pv_power_kw=2., plant_load_power_kw=.35, plant_battery_soc_percent=50.,
                    plant_grid_power_kw=2. if minute == 0 else -1.))
            session.add(SigenStorModbusSample(collected_at_utc=start + timedelta(seconds=30), plant_grid_power_kw=-2.))
        actuals = load_plan_actuals(db, start, start + timedelta(minutes=2), 18.)
        assert len(actuals) == 3
        assert actuals.solar.iloc[0] == 2.
        assert actuals.house_load.iloc[0] == .35
        assert actuals.battery.iloc[0] == 9.
        assert actuals.grid_import.iloc[0] == actuals.grid_export.iloc[0] == 1.
        assert actuals.grid_import.iloc[-1] == 0.
        assert actuals.grid_export.iloc[-1] == 1.
        assert pd.isna(actuals.solar.iloc[1])
        assert actuals.timestamp.iloc[-1] == start + timedelta(minutes=2)
    finally:
        db.close()


@pytest.mark.parametrize("fails", [False, True])
def test_preference_is_request_local_and_database_is_closed(monkeypatch, fails):
    from solar_model.services import planning
    closed = []
    fake_db = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(planning, "SolarDatabase", lambda engine: fake_db)
    monkeypatch.setattr(planning, "create_engine_for_database", lambda path: None)
    monkeypatch.setattr(planning, "load_device_info_as_of", lambda *args: SimpleNamespace(system_battery_capacity_kwh=18.))
    monkeypatch.setattr(planning, "load_plan_actuals", lambda *args: pd.DataFrame())
    config = load_config()
    original = config.energy_plan.protection_preference
    result = sample_result()

    def plan(db, configured, when):
        assert configured.energy_plan.protection_preference == 75.
        if fails:
            raise ValueError("missing forecast")
        return result

    monkeypatch.setattr(planning, "plan_as_of", plan)
    if fails:
        with pytest.raises(ValueError, match="missing forecast"):
            planning.plan_with_preference(config, datetime.now(timezone.utc), 75.)
    else:
        assert planning.plan_with_preference(config, datetime.now(timezone.utc), 75.).simulation is result
    assert config.energy_plan.protection_preference == original
    assert closed == [True]


def test_real_report_from_off_hour_inputs_excludes_future_data(tmp_path):
    from dataclasses import fields
    from solar_model.planning.types import DeviceInfo
    from solar_model.services.planning import plan_with_preference
    from solar_model.storage.migrations import open_database
    from solar_model.storage.schema import ForecastSolarSample, SigenStorDevice, SigenStorHourlySample, SigenStorModbusSample
    config = load_config()
    config = replace(config, timezone="UTC", database=replace(config.database, path=str(tmp_path / "real-plan.db")),
                     forecast=replace(config.forecast, solar=replace(config.forecast.solar, arrays=(config.forecast.solar.arrays[0],))),
                     energy_plan=replace(config.energy_plan, num_projection_hours=1))
    start = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)
    device = DeviceInfo(**{field.name: 1. for field in fields(DeviceInfo)})
    device = replace(device, system_battery_capacity_kwh=18., battery_state_of_health_percent=100.,
                     inverter_max_active_power_kw=5., inverter_max_absorption_power_kw=5.,
                     inverter_battery_charge_power_kw=5., inverter_battery_discharge_power_kw=5.)
    db = open_database(config.database.path)
    try:
        with db.sessions.begin() as session:
            for field in fields(device):
                session.add(SigenStorDevice(collected_at_utc=(start - timedelta(minutes=1)).isoformat(),
                    collected_at_local=start.isoformat(), variable=field.name, variable_name=field.name,
                    value=str(getattr(device, field.name)), unit=""))
            for stamp in pd.date_range("2026-10-25T00:00Z", "2026-10-27T00:00Z", freq="h", inclusive="left"):
                session.add(SigenStorHourlySample(hour_start_utc=stamp.isoformat(), import_tariff_band="day",
                    sample_count=1, house_load_kwh_period=.35))
            for delta, scale in ((-1, 1.), (1, 100.)):
                for hour in range(3):
                    session.add(ForecastSolarSample(collected_at_utc=start + timedelta(minutes=delta),
                        forecast_time=start.replace(minute=0).isoformat() if hour == 0 else (start.replace(minute=0) + timedelta(hours=hour)).isoformat(),
                        **{f"panel_{config.forecast.solar.arrays[0].panel_id}_adj_watt_hours": hour * 1000. * scale}))
            session.add(SigenStorModbusSample(collected_at_utc=start, plant_battery_soc_percent=50.,
                                             plant_pv_power_kw=1., plant_load_power_kw=.35))
    finally:
        db.close()
    report = plan_with_preference(config, start, 0.)
    assert len(report.simulation.results) == 9
    base = report.simulation.results["base"]
    assert base.starts_at.iloc[0] == start
    assert base.ends_at.iloc[-1] == start + timedelta(hours=1)
    assert base.solar_kwh.sum() == pytest.approx(1.)
    assert base.load_kwh.sum() == pytest.approx(.35)
    assert report.actuals.battery.iloc[-1] == 9.
