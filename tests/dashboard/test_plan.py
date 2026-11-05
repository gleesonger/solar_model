from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from solar_model.config import load_config
from solar_model.dashboard.tabs.plan import advanced_detail_totals, detail_frame, detail_rows, detail_totals, hourly_detail_frame, plan_chart_options, scenario_summary, table_detail_frame
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
        "battery_charge_loss_kwh": [.01, .02], "inverter_ac_to_dc_loss_kwh": [.02, .03],
        "battery_discharge_loss_kwh": [.03, .04], "inverter_dc_to_ac_loss_kwh": [.01, .02], "solar_clipped_kwh": [0., .1],
        "import_cost": [.12, .32], "export_revenue": [0., .02],
        "cost_score": [-1., .25], "protection_score": [.01, .02], "total_score": [-.99, .27],
    })
    action = NextAction(OperatingMode.SELF_CONSUMPTION, 0., 0.)
    return SimulationResult(action, {"med": frame, "low_high": frame.assign(net_cost=2.)})


@pytest.mark.parametrize("model,adjustment", [("MILP", 75.), ("DP", 75.5), ("DP", 0.)])
@pytest.mark.parametrize("solar,load", [("med", "med"), ("high", "low")])
@pytest.mark.parametrize("view,abbreviation", [("Compact", "com"), ("Advanced", "adv")])
def test_excel_filename_uses_applied_model_adjustment_scenario_and_plan_date(monkeypatch, model, adjustment, solar, load, view, abbreviation):
    from solar_model.dashboard.tabs import plan

    tab = plan.PlanTab(load_config())
    tab.result = sample_result()
    tab.applied_model, tab.applied_adjustment = model, adjustment
    tab.detail_view = view
    tab.solar_scenario, tab.load_scenario = SimpleNamespace(value=solar), SimpleNamespace(value=load)
    tab.model = SimpleNamespace(value="DP")
    downloads = []
    exported_frames = []

    def capture_workbook(frame, **kwargs):
        exported_frames.append(frame)
        return b"workbook"

    monkeypatch.setattr(plan, "excel_bytes", capture_workbook)
    monkeypatch.setattr(plan.ui, "download", lambda content, **kwargs: downloads.append(kwargs))
    frame = tab.result.results[tab.scenario_name].assign(limit_score=[-2., -3.], plan_action=["Force discharge", "Force charge"])
    tab._download_detail(frame, plan.detail_columns(frame, compact=view == "Compact"))
    assert downloads[0]["filename"] == f"plan-{model}-{abbreviation}-protection-{adjustment:g}-solar-{solar}-load-{load}-20261027-1005.xlsx"
    assert exported_frames[0].Start.iloc[0] == "2026-10-27 10:05:00"
    assert exported_frames[0].End.iloc[0] == "2026-10-27 10:20:00"
    action_column = "Action"
    expected_actions = ["Force discharge", "Force charge", None, None, None] if view == "Advanced" else ["Force discharge", "Force charge", None]
    assert exported_frames[0][action_column].tolist() == expected_actions
    if view == "Advanced":
        assert exported_frames[0].Start.tolist()[-3:] == ["0–24h", "24–48h", "Total"]
        assert exported_frames[0]["Limit score"].iloc[-3:].tolist()[0] == -5.
        assert pd.isna(exported_frames[0]["Limit score"].iloc[-2])
        assert exported_frames[0]["Limit score"].iloc[-1] == -5.
        assert not {"Battery start (kWh)", "Battery start SOC (%)", "Solar To Inverter kWh", "Battery To Inverter kWh", "Battery Charge Loss kWh"} & set(exported_frames[0])
    else:
        assert "Limit Score" not in exported_frames[0]
    assert plan.detail_rows(frame, tab.config.timezone)[0]["starts_at"] == "10:05"


def test_column_metadata_controls_views_and_keeps_unknown_advanced_fields():
    from solar_model.dashboard.tabs.plan import detail_columns

    frame = sample_result().results["med"].assign(extra_power_kw=1.)
    compact = detail_columns(frame, compact=True)
    names = [column["name"] for column in compact]
    assert names[:9] == ["starts_at", "ends_at", "t", "plan_action", "tariff_name",
                         "import_rate", "export_rate", "load_kwh", "solar_kwh"]
    assert "extra_power_kw" not in names
    assert next(column for column in compact if column["name"] == "load_kwh")["label"] == "House (kWh)"
    advanced = detail_columns(frame, compact=False)
    assert next(column for column in advanced if column["name"] == "load_kwh")["label"] == "Load\nkWh"
    assert next(column for column in advanced if column["name"] == "t")["label"] == "hours"
    assert advanced[-1]["name"] == "extra_power_kw"
    assert all(column["sortable"] is False for column in advanced)


def test_advanced_columns_show_only_summary_losses_and_short_flow_names():
    from solar_model.dashboard.tabs.plan import ADVANCED_HIDDEN_COLUMNS, detail_columns

    frame = detail_frame(sample_result().results["med"].assign(
        solar_to_load_kwh=[.1, .2], solar_to_battery_kwh=[.2, .3],
        solar_to_inverter_kwh=[.3, .5], battery_to_inverter_kwh=[0., .1],
    ), 20.)
    columns = {column["name"]: column for column in detail_columns(frame, compact=False)}
    assert not ADVANCED_HIDDEN_COLUMNS & columns.keys()
    assert columns["solar_to_load_kwh"]["label"] == "S→L\nkWh"
    assert columns["solar_to_battery_kwh"]["label"] == "S→B\nkWh"
    assert columns["losses_kwh"]["label"] == "Losses\nkWh"
    assert frame.losses_kwh.tolist() == pytest.approx([.07, .11])


def test_scenario_average_cost_uses_joint_probabilities():
    from solar_model.dashboard.tabs.plan import scenario_average_cost
    from solar_model.planning.scenarios import scenario_probabilities

    settings = load_config().energy_plan.scenarios
    results = {name: pd.DataFrame({"net_cost": [10. if name == "med" else 0.]})
               for name in scenario_probabilities(settings)}
    assert scenario_average_cost(results, settings) == pytest.approx(3.6)


def test_aggregation_uses_metadata_instead_of_column_units(monkeypatch):
    from solar_model.dashboard.tabs.plan import Aggregation, PLAN_COLUMNS, PlanColumn

    # A kWh setting must not be prorated/summed; a kW average must be weighted.
    monkeypatch.setitem(PLAN_COLUMNS, "configured_kwh", PlanColumn(
        "configured_kwh", "Configured", None, None, "number", 2, Aggregation.SETTING, False, True))
    monkeypatch.setitem(PLAN_COLUMNS, "power_kw", PlanColumn(
        "power_kw", "Power", None, None, "number", 2, Aggregation.AVERAGE, False, True))
    frame = sample_result().results["med"].assign(configured_kwh=[10., 20.], power_kw=[2., 8.])
    hourly = hourly_detail_frame(frame, "UTC", OperatingMode.SELF_CONSUMPTION)
    assert hourly.configured_kwh.tolist() == [20., 20.]
    assert hourly.power_kw.iloc[0] == pytest.approx((2 * 15 + 8 * 40) / 55)
    assert hourly.power_kw.iloc[1] == 8.
    assert detail_totals(frame)["configured_kwh"] is None
    assert detail_totals(frame)["power_kw"] is None
    assert hourly.solar_kwh.sum() == pytest.approx(frame.solar_kwh.sum())


def test_summary_and_chart_use_duration_weighting_and_consistent_units():
    from solar_model.planning.plan_optimisation_milp import battery_protection_score
    frame = sample_result().results["med"]
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
    for index in (7, 9):
        assert options["series"][index]["step"] == "end"
        assert options["series"][index]["smooth"] is False


def test_detail_rows_show_local_time_only_and_preserve_missing_values():
    frame = sample_result().results["med"].copy()
    frame["starts_at"] = pd.to_datetime(["2026-10-25T00:30Z", "2026-10-25T01:30Z"])
    frame.loc[0, "solar_kwh"] = float("nan")
    rows = detail_rows(frame, "Europe/Dublin")
    assert rows[0]["starts_at"] == "01:30"
    assert rows[1]["starts_at"] == "01:30"
    assert rows[0]["period"] != rows[1]["period"]
    assert rows[0]["solar_kwh"] is None
    assert rows[1]["solar_kwh"] == 2.
    assert frame.solar_kwh.isna().iloc[0]


def test_table_scores_keep_terminal_and_total_as_period_contributions():
    original = sample_result().results["med"].assign(limit_score=[-2., -3.], terminal_score=[0., 4.], total_score=[-2.99, 1.27])
    display = table_detail_frame(original)
    assert display.t.tolist() == [.25, 1.]
    assert display.cost_score.tolist() == [-4., .25]
    assert display.protection_score.tolist() == [.04, .02]
    assert display.limit_score.tolist() == [-8., -3.]
    for column in ("cost_score", "protection_score", "limit_score"):
        assert (display[column] * display.t).sum() == pytest.approx(original[column].sum())
    assert display.terminal_score.tolist() == [0., 4.]
    assert display.total_score.tolist() == [-2.99, 1.27]
    assert "t" not in original
    assert original.protection_score.tolist() == [.01, .02]


def test_partial_final_interval_does_not_scale_terminal_credit():
    start = pd.Timestamp("2026-11-07T10:00:00Z")
    frame = pd.DataFrame({
        "starts_at": [start], "ends_at": [start + pd.Timedelta(minutes=9, seconds=39)],
        "cost_score": [0.], "protection_score": [.0029],
        "terminal_score": [4.6943], "total_score": [4.6972],
    })
    display = table_detail_frame(frame)
    assert display.t.iloc[0] == pytest.approx(579 / 3600)
    assert display.protection_score.iloc[0] == pytest.approx(.0029 / (579 / 3600))
    assert display.terminal_score.iloc[0] == 4.6943
    assert display.total_score.iloc[0] == 4.6972


def test_detail_totals_sum_flows_and_costs_without_summing_levels_or_rates():
    frame = sample_result().results["med"].assign(
        minimum_battery_kwh=4., maximum_battery_kwh=18., import_rate=.4,
        limit_battery_export_rate_to_grid_kw=5., solar_clipped_kwh=float("nan"),
    )
    totals = detail_totals(frame)
    assert totals["starts_at"] == "Total"
    assert totals["solar_kwh"] == 2.5
    assert totals["load_kwh"] == 1.25
    assert totals["net_cost"] == .75
    assert totals["cost_score"] == -.75
    assert totals["protection_score"] == .03
    assert totals["total_score"] == -.72
    for column in ("ends_at", "battery_start_kwh", "battery_end_kwh", "minimum_battery_kwh",
                   "maximum_battery_kwh", "import_rate", "limit_battery_export_rate_to_grid_kw", "solar_clipped_kwh"):
        assert totals[column] is None


def test_advanced_totals_split_crossing_interval_and_keep_terminal_credit_at_end():
    start = pd.Timestamp("2026-11-05T00:00:00Z")
    frame = pd.DataFrame({
        "starts_at": [start, start + pd.Timedelta(hours=23)],
        "ends_at": [start + pd.Timedelta(hours=23), start + pd.Timedelta(hours=26)],
        "solar_kwh": [23., 6.], "cost_score": [-23., -6.],
        "terminal_score": [0., 4.], "total_score": [-23., -2.], "t": [23., 3.],
        "battery_end_kwh": [4., 3.],
    })
    first, second, total = advanced_detail_totals(frame)
    assert [row["starts_at"] for row in (first, second, total)] == ["0–24h", "24–48h", "Total"]
    assert [row["t"] for row in (first, second, total)] == [24., 2., 26.]
    assert [row["solar_kwh"] for row in (first, second, total)] == [25., 4., 29.]
    assert [row["terminal_score"] for row in (first, second, total)] == [0., 4., 4.]
    assert [row["total_score"] for row in (first, second, total)] == [-25., 0., -25.]
    assert all(row["battery_end_kwh"] is None for row in (first, second, total))


def test_detail_actions_use_shared_first_mode_and_later_grid_battery_flows():
    frame = sample_result().results["med"]
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
    original = sample_result().results["med"]
    frame = detail_frame(original, 20.)
    assert frame.battery_start_soc_percent.tolist() == pytest.approx([20., 21.])
    assert frame.battery_end_soc_percent.tolist() == pytest.approx([21., 15.])
    assert frame.columns.get_loc("battery_end_soc_percent") == frame.columns.get_loc("battery_end_kwh") + 1
    assert detail_totals(frame)["battery_end_soc_percent"] is None
    assert frame.losses_kwh.tolist() == pytest.approx([.07, .11])
    assert detail_totals(frame)["losses_kwh"] == .18
    assert detail_frame(original, None).battery_end_soc_percent.isna().all()
    assert "battery_end_soc_percent" not in original.columns


def test_compact_hours_split_flows_preserve_totals_and_interpolate_battery():
    frame = detail_frame(sample_result().results["med"], 20.)
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
    frame = sample_result().results["med"].iloc[[0]].copy()
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
@pytest.mark.parametrize("preference", [75., 200.])
@pytest.mark.parametrize("model", ["MILP", "DP"])
def test_preference_is_request_local_and_database_is_closed(monkeypatch, fails, preference, model):
    from solar_model.services import planning
    closed = []
    fake_db = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(planning, "SolarDatabase", lambda engine: fake_db)
    monkeypatch.setattr(planning, "create_engine_for_database", lambda path: None)
    monkeypatch.setattr(planning, "load_device_info_as_of", lambda *args: SimpleNamespace(system_battery_capacity_kwh=18., battery_state_of_health_percent=90.))
    monkeypatch.setattr(planning, "load_plan_actuals", lambda *args: pd.DataFrame())
    monkeypatch.setattr(planning, "load_latest_full_update", lambda *args: None)
    config = load_config()
    original = config.energy_plan.protection_preference
    result = sample_result()

    def plan(db, configured, when, **kwargs):
        assert configured.energy_plan.protection_preference == preference
        assert configured.energy_plan.optimiser.model == ("mix_integer_linear_prog" if model == "MILP" else "dynamic_programming")
        if fails:
            raise ValueError("missing forecast")
        return result

    monkeypatch.setattr(planning, "plan_as_of", plan)
    if fails:
        with pytest.raises(ValueError, match="missing forecast"):
            planning.generate_plan_report(config, datetime.now(timezone.utc), replace(config.energy_plan, protection_preference=preference, optimiser=replace(config.energy_plan.optimiser, model="mix_integer_linear_prog" if model == "MILP" else "dynamic_programming")))
    else:
        assert planning.generate_plan_report(config, datetime.now(timezone.utc), replace(config.energy_plan, protection_preference=preference, optimiser=replace(config.energy_plan.optimiser, model="mix_integer_linear_prog" if model == "MILP" else "dynamic_programming"))).simulation is result
    assert config.energy_plan.protection_preference == original
    assert closed == [True]


@pytest.mark.parametrize("model", [None, "MILP", "DP"])
@pytest.mark.parametrize("battery_lag", [0, 60])
def test_planning_service_dispatches_the_selected_model_with_shared_inputs(monkeypatch, model, battery_lag):
    from solar_model.services import planning
    from solar_model.planning import plan_optimisation_dynamic_programming as dp

    config, database = load_config(), object()
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    device, state, forecast, scenarios = object(), object(), object(), [object()]
    monkeypatch.setattr(planning, "load_device_info_as_of", lambda db, cfg, when: device)
    battery_cutoffs = []

    def battery_state(db, when, info):
        battery_cutoffs.append(when)
        return state

    monkeypatch.setattr(planning, "load_battery_state_as_of", battery_state)
    monkeypatch.setattr(planning, "load_forecast_as_of", lambda db, when, cfg: forecast)
    monkeypatch.setattr(planning, "create_scenarios", lambda data, settings: scenarios)
    calls = []
    result = sample_result()

    def milp(*args):
        calls.append(("MILP", args))
        return result

    def dynamic_programming(*args):
        calls.append(("DP", args))
        return result

    monkeypatch.setattr(planning, "simulate_energy_plan", milp)
    monkeypatch.setattr(dp, "simulate_energy_plan", dynamic_programming)
    if model is not None:
        config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, model="mix_integer_linear_prog" if model == "MILP" else "dynamic_programming")))
    battery_as_of = start - timedelta(seconds=battery_lag) if battery_lag else None
    assert planning.plan_as_of(database, config, start, battery_as_of=battery_as_of) is result
    assert battery_cutoffs == [battery_as_of or start]
    selected = "MILP" if config.energy_plan.optimiser.model == "mix_integer_linear_prog" else "DP"
    assert calls == [(selected, (device, state, scenarios, config, start))]


def test_invalid_planning_model_is_rejected_before_opening_database(monkeypatch):
    from solar_model.services import planning

    def unexpected_database(*args):
        raise AssertionError("invalid model must not open the database")

    monkeypatch.setattr(planning, "create_engine_for_database", unexpected_database)
    config = load_config()
    config = replace(config, energy_plan=replace(config.energy_plan, optimiser=replace(config.energy_plan.optimiser, model="unknown")))
    with pytest.raises(ValueError, match="optimiser.model"):
        planning.generate_plan_report(config, datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="optimiser.model"):
        planning.plan_as_of(None, config, datetime.now(timezone.utc))


def test_real_report_from_off_hour_inputs_excludes_future_data(tmp_path):
    from dataclasses import fields
    from solar_model.planning.types import DeviceInfo
    from solar_model.services.planning import generate_plan_report
    from solar_model.storage.migrations import open_database
    from solar_model.storage.schema import ForecastSolarSample, SigenStorDevice, SigenStorHourlySample, SigenStorModbusSample
    config = load_config()
    config = replace(config, timezone="UTC", database=replace(config.database, path=str(tmp_path / "real-plan.db")),
                     forecast=replace(config.forecast, solar=replace(config.forecast.solar, arrays=(config.forecast.solar.arrays[0],))),
                     dashboard=replace(config.dashboard, actuals_to_forecast={"pv1": config.forecast.solar.arrays[0].name}),
                     energy_plan=replace(config.energy_plan, num_projection_hours=25, num_visible_projection_hours=1))
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
    report = generate_plan_report(config, start, replace(config.energy_plan, protection_preference=0.))
    assert len(report.simulation.results) == 9
    base = report.simulation.results["med"]
    assert base.starts_at.iloc[0] == start
    assert base.ends_at.iloc[-1] == start + timedelta(hours=1)
    assert base.solar_kwh.sum() == pytest.approx(1.)
    assert base.load_kwh.sum() == pytest.approx(.35)
    assert report.actuals.battery.iloc[-1] == 9.


def test_ui_overlay_is_local_and_failed_save_keeps_previous_settings(monkeypatch, tmp_path):
    from solar_model.dashboard.tabs import plan
    from solar_model.config import energy_plan_yaml

    config = load_config()
    first, other = plan.PlanTab(config, str(tmp_path / "missing.yaml")), plan.PlanTab(config)
    initial = replace(config.energy_plan, protection_preference=75.)
    first.apply_plan_settings(energy_plan_yaml(initial))
    assert first.energy_plan_override == initial
    assert other.energy_plan_override is None
    assert config.energy_plan.protection_preference == 50
    with pytest.raises(OSError):
        first.save_plan_settings(energy_plan_yaml(replace(initial, protection_preference=123.)))
    assert first.energy_plan_override == initial


def test_ui_report_without_override_uses_global_settings(monkeypatch):
    from solar_model.services import planning

    config = load_config()
    monkeypatch.setattr(planning, "create_engine_for_database", lambda path: None)
    monkeypatch.setattr(planning, "SolarDatabase", lambda engine: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(planning, "load_device_info_as_of", lambda *args: SimpleNamespace(system_battery_capacity_kwh=18., battery_state_of_health_percent=90.))
    monkeypatch.setattr(planning, "load_plan_actuals", lambda *args: pd.DataFrame())
    monkeypatch.setattr(planning, "load_latest_full_update", lambda *args: None)

    def calculate(database, configured, as_of, **kwargs):
        assert configured is config
        return sample_result()

    monkeypatch.setattr(planning, "plan_as_of", calculate)
    report = planning.generate_plan_report(config, datetime.now(timezone.utc))
    assert report.battery_capacity_kwh == 18.
    assert report.command_battery_capacity_kwh == pytest.approx(16.2)
    assert report.scenarios is config.energy_plan.scenarios
    assert report.preferred_reserve_kwh == config.energy_plan.preferred_reserve_kwh
    assert report.protection_preference == config.energy_plan.protection_preference


def test_net_cost_summary_changes_selected_cost_but_preserves_range():
    from solar_model.dashboard.tabs.plan import scenario_cost_summary
    from solar_model.planning.scenarios import scenario_probabilities

    settings = load_config().energy_plan.scenarios
    results = {name: pd.DataFrame({"net_cost": [.25]}) for name in scenario_probabilities(settings)}
    results["low_low"] = pd.DataFrame({"net_cost": [-2.44]})
    results["high_high"] = pd.DataFrame({"net_cost": [2.40]})
    assert scenario_cost_summary(results, "med") == "\u20ac0.250 (-\u20ac2.44 to \u20ac2.40)"
    assert scenario_cost_summary(results, "low_low") == "-\u20ac2.440 (-\u20ac2.44 to \u20ac2.40)"
    assert scenario_cost_summary(results, "high_high") == "\u20ac2.400 (-\u20ac2.44 to \u20ac2.40)"
    negative = {name: frame.assign(net_cost=-1.) for name, frame in results.items()}
    assert scenario_cost_summary(negative, "med") == "-\u20ac1.000 (-\u20ac1.00 to -\u20ac1.00)"


def test_advanced_plan_details_use_period_power_and_projected_battery_targets():
    from solar_model.dashboard.tabs.plan import advanced_plan_frame

    original = sample_result().results["med"].assign(grid_export_kwh=[.5, .1], battery_to_grid_kwh=[0., .1])
    frame = detail_frame(original, 20.)
    advanced = advanced_plan_frame(frame, "Europe/Dublin", OperatingMode.DISCHARGE_PV_FIRST)
    assert advanced.plan_action.tolist() == [
        "Force discharge: export 2.00 kW to 4kWh/21%",
        "Force discharge: export 0.10 kW to 3kWh/15%",
    ]
    assert detail_rows(advanced, "Europe/Dublin")[0]["plan_action"] == advanced.plan_action.iloc[0]
    assert detail_totals(advanced)["plan_action"] is None
    assert "plan_action" not in frame and "plan_action" not in original
    missing_capacity = advanced_plan_frame(detail_frame(original, None), "Europe/Dublin", OperatingMode.DISCHARGE_PV_FIRST)
    assert "to 4kWh" in missing_capacity.plan_action.iloc[0]
    assert "SoC" not in missing_capacity.plan_action.iloc[0]


def test_advanced_charge_details_use_import_power_for_shared_and_inferred_actions():
    from solar_model.dashboard.tabs.plan import advanced_plan_frame

    original = sample_result().results["med"].assign(grid_export_kwh=0., grid_to_battery_kwh=[0., .6])
    advanced = advanced_plan_frame(detail_frame(original, 20.), "Europe/Dublin", OperatingMode.CHARGE_PV_FIRST)
    assert advanced.plan_action.tolist() == [
        "Force charge: import 1.20 kW to 4kWh/21%",
        "Force charge: import 0.80 kW to 3kWh/15%",
    ]
    assert detail_rows(advanced, "Europe/Dublin")[0]["plan_action"] == advanced.plan_action.iloc[0]
    assert "plan_action" not in original


def test_advanced_self_consumption_has_no_flow_or_battery_suffix():
    from solar_model.dashboard.tabs.plan import advanced_plan_frame

    frame = detail_frame(sample_result().results["med"], 20.)
    advanced = advanced_plan_frame(frame, "Europe/Dublin", OperatingMode.SELF_CONSUMPTION)
    assert advanced.plan_action.iloc[0] == "Self-consumption"
    assert "plan_action" not in frame


@pytest.mark.parametrize("wrapped", [False, True])
def test_infeasible_refresh_reports_message_without_traceback(monkeypatch, caplog, wrapped):
    import asyncio
    from nicegui.run import safe_callback
    from solar_model.dashboard.tabs import plan

    tab = plan.PlanTab(load_config())
    previous = sample_result()
    tab.result = previous
    status, notices = [], []
    control = SimpleNamespace(
        set_visibility=lambda value: None,
        disable=lambda: None,
        enable=lambda: None,
    )
    tab.loading = tab.settings_button = control
    tab.status = SimpleNamespace(set_visibility=lambda value: None, set_text=status.append)

    def fail():
        raise plan.NoFeasiblePlanError("unable to find a feasible energy plan on the dynamic-programming grid")

    async def calculate(*args):
        return safe_callback(fail) if wrapped else fail()

    async def warm():
        pass

    monkeypatch.setattr(plan.run, "cpu_bound", calculate)
    monkeypatch.setattr(plan, "ensure_planning_worker", warm)
    monkeypatch.setattr(plan, "notify_error", notices.append)
    asyncio.run(tab.run_demonstration())
    assert notices == ["Unable to calculate plan: unable to find a feasible energy plan on the dynamic-programming grid"]
    assert status == ["Plan refresh failed. The previous plan is still shown."]
    assert tab.result is previous and not tab.running
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("mode,soc,capacity,expected", [
    (OperatingMode.SELF_CONSUMPTION, None, 18., "Self-consumption"),
    (OperatingMode.DISCHARGE_PV_FIRST, None, 18., "Force discharge at 2.85 kW"),
    (OperatingMode.DISCHARGE_PV_FIRST, 0., 18., "Force discharge at 2.85 kW; target 0.00 kWh (0.0%)"),
    (OperatingMode.DISCHARGE_PV_FIRST, 20., 18., "Force discharge at 2.85 kW; target 3.60 kWh (20.0%)"),
    (OperatingMode.CHARGE_PV_FIRST, 80., 18., "Force charge at 2.85 kW; target 14.40 kWh (80.0%)"),
    (OperatingMode.DISCHARGE_PV_FIRST, 20., None, "Force discharge at 2.85 kW; target 20.0%"),
])
def test_next_action_summary_uses_operating_mode_label(mode, soc, capacity, expected):
    from solar_model.dashboard.tabs import plan
    from solar_model.services.planning import PlanReport

    tab = plan.PlanTab(load_config())
    tab.result = sample_result()
    action = replace(
        tab.result.next_action,
        mode=mode,
        charge_power_limit_kw=2.85,
        discharge_power_limit_kw=2.85,
        target_soc_percent=soc,
    )
    tab.result = replace(tab.result, next_action=action)
    tab.report = PlanReport(tab.result, pd.DataFrame(), battery_capacity_kwh=20., command_battery_capacity_kwh=capacity)
    labels = []
    tab.chart = object()
    tab.action_period_label = SimpleNamespace(set_text=lambda text: None)
    tab.action_label = SimpleNamespace(set_text=labels.append)
    tab.solar_scenario = tab.load_scenario = SimpleNamespace(options={}, update=lambda: None)
    tab._render_scenario = lambda: None
    tab._render_result()
    assert labels == [expected]


def test_policy_labels_survive_detail_grouping_and_advanced_formatting():
    from solar_model.dashboard.tabs.plan import advanced_plan_frame

    frame = detail_frame(sample_result().results["med"], 20.).assign(plan_action=["Force discharge", "Force charge"])
    assert [row["plan_action"] for row in detail_rows(frame, "Europe/Dublin", OperatingMode.CHARGE_PV_FIRST)] == ["Force discharge", "Force charge"]
    hourly = hourly_detail_frame(frame, "Europe/Dublin", OperatingMode.SELF_CONSUMPTION)
    assert hourly.plan_action.tolist() == ["Force discharge / Force charge", "Force charge"]
    advanced = advanced_plan_frame(frame, "Europe/Dublin")
    assert advanced.plan_action.tolist() == ["Force discharge: export 0.00 kW to 4kWh/21%", "Force charge: import 0.80 kW to 3kWh/15%"]
    assert detail_totals(frame)["plan_action"] is None


@pytest.mark.parametrize("mode,label", [
    (OperatingMode.CHARGE_PV_FIRST, "Force charge"),
    (OperatingMode.DISCHARGE_PV_FIRST, "Force discharge"),
])
def test_zero_rate_solar_export_uses_display_only_label(mode, label):
    from solar_model.dashboard.tabs.plan import advanced_plan_frame, next_action_description

    original = sample_result().results["med"].iloc[:1].copy().assign(
        plan_action=label, solar_to_grid_kwh=.2, solar_to_battery_kwh=0.,
        grid_to_battery_kwh=0., battery_to_inverter_kwh=0., grid_export_kwh=.2,
    )
    frame = detail_frame(original, 20.)
    zero = NextAction(mode, 0., 0.)
    assert detail_rows(frame, "Europe/Dublin", zero)[0]["plan_action"] == "Export Solar Excess"
    assert hourly_detail_frame(frame, "Europe/Dublin", zero).plan_action.iloc[0] == "Export Solar Excess"
    assert advanced_plan_frame(frame, "Europe/Dublin", zero).plan_action.iloc[0].startswith("Export Solar Excess: export")
    assert frame.plan_action.iloc[0] == label
    positive = NextAction(mode, 1., 1.)
    assert detail_rows(frame, "Europe/Dublin", positive)[0]["plan_action"] == label
    assert detail_rows(frame.assign(solar_to_grid_kwh=0.), "Europe/Dublin", zero)[0]["plan_action"] == label
    assert next_action_description(zero, .2, 20.) == "Export Solar Excess"
    assert next_action_description(zero, 0., 20.) == f"{label} at 0.00 kW"
    assert next_action_description(positive, .2, 20.) == f"{label} at 1.00 kW"
