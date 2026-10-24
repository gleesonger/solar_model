from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from solar_model.config import ForecastModelConfig, ForecastModelsConfig, load_config
import solar_model.tools.evaluate_load as load
import solar_model.planning.forecasts.solar as solar


@pytest.fixture
def config_data():
    return yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text(encoding="utf-8"))


def write_config(tmp_path, data):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_yaml_model_settings_and_legacy_defaults(tmp_path, config_data):
    custom = ForecastModelsConfig(
        solar_day_ahead=ForecastModelConfig(7., 2., None),
        solar_intraday=ForecastModelConfig(2., 0.5, 0.5),
        load=ForecastModelConfig(14., 3., 0.25),
    )
    config_data["forecast"]["model_calibration_parameters"] = asdict(custom)
    config = load_config(write_config(tmp_path, config_data))
    assert config.forecast.model_calibration_parameters == custom
    assert config.forecast.model_calibration_parameters.load.prior_strength == 3.
    del config_data["forecast"]["model_calibration_parameters"]
    legacy = load_config(write_config(tmp_path, config_data)).forecast.model_calibration_parameters
    assert legacy == ForecastModelsConfig()
    assert legacy.load.prior_strength == 4.
    assert legacy.solar_intraday.prior_strength == 2.


@pytest.mark.parametrize("model,key,value", [
    ("load", "lookback", 0), ("load", "lookback", None),
    ("load", "halflife", -1), ("solar_intraday", "halflife", float("inf")),
    ("load", "prior_effective_observations", 0),
    ("load", "prior_effective_observations", 20),
    ("load", "prior_effective_observations", float("nan")),
    ("solar_intraday", "prior_effective_observations", None),
    ("solar_day_ahead", "prior_effective_observations", 0.2),
    ("solar_day_ahead", "lookback", None),
])
def test_invalid_yaml_model_settings_rejected(tmp_path, config_data, model, key, value):
    config_data["forecast"]["model_calibration_parameters"][model][key] = value
    with pytest.raises(ValueError, match="invalid configuration"):
        load_config(write_config(tmp_path, config_data))


def test_load_lookback_changes_eligible_history():
    from tests.forecasting.test_forecast_influence import load_history

    # The short window excludes the older weekend observation.
    result = load.predict_day(load_history(), date(2026, 10, 23), ZoneInfo("UTC"), ForecastModelConfig(2., 1., 0.2))
    assert result.loc[0, "prediction_kwh"] == 2.


def test_solar_day_ahead_settings_change_scalar_calibration():
    as_of = pd.Timestamp("2026-10-23T10:00Z")
    training = pd.DataFrame({
        "target": [as_of - pd.Timedelta(days=1), as_of - pd.Timedelta(days=3)],
        "raw_total_kw": [1., 1.], "plant_pv_power_kw": [2., 10.],
    })
    short = solar._fit_scalars(training, as_of, ForecastModelConfig(2., 1., None))
    long = solar._fit_scalars(training, as_of, ForecastModelConfig(56., 1., None))
    assert short[10] == 2.
    assert long[10] == pytest.approx(3.6)


@pytest.mark.parametrize("halflife,expected", [(1., 5 / 3), (0.5, 1.4)])
def test_intraday_lookback_and_halflife(halflife, expected):
    issued = pd.DataFrame({
        "target_hour": pd.to_datetime(["2026-10-23T09:00Z", "2026-10-23T08:00Z"]),
        "raw_total_kw": [1., 1.], "plant_pv_power_kw": [2., 4.], "baseline_kw": [1., 1.],
    })
    as_of = pd.Timestamp("2026-10-23T10:00Z")
    distributions = {8: (1., 1.), 9: (1., 1.)}
    score, _, count = solar._today_residual(issued, distributions, as_of, ForecastModelConfig(None, halflife, 0.5))
    assert count == 2
    assert score == pytest.approx(expected)
    limited = solar._today_residual(issued, distributions, as_of, ForecastModelConfig(1.5, halflife, 0.5))
    assert limited == (1., 0.5, 1)


def test_load_cli_reads_model_database_and_timezone_from_config(tmp_path, config_data, monkeypatch):
    config_data["database"]["path"] = "configured.db"
    config_data["timezone"] = "Asia/Kolkata"
    config_data["forecast"]["model_calibration_parameters"]["load"] = {"lookback": 9., "halflife": 3., "prior_effective_observations": 0.5}
    path = write_config(tmp_path, config_data)
    history = pd.DataFrame()
    read = Mock(return_value=history)
    evaluate = Mock(return_value=pd.DataFrame())
    monkeypatch.setattr(load, "load_hourly_load", read)
    monkeypatch.setattr(load, "evaluate", evaluate)
    monkeypatch.setattr("sys.argv", ["load-profile", "--config", str(path), "--start", "2026-10-20", "--end", "2026-10-23"])
    load.main()
    read.assert_called_once_with("configured.db", ZoneInfo("Asia/Kolkata"))
    assert evaluate.call_args.args[4] == ForecastModelConfig(9., 3., 0.5)


def test_refresh_forwards_configured_models(monkeypatch):
    import solar_model.services.forecasting as solar
    models = ForecastModelsConfig(solar_intraday=ForecastModelConfig(2., 0.5, 0.5))
    as_of = pd.Timestamp("2026-10-23T10:00Z")
    frames = pd.DataFrame({"snapshot": [as_of], "target_hour": [as_of]})
    monkeypatch.setattr(solar, "load_operational_inputs", Mock(return_value=(frames, pd.DataFrame())))
    calculator = Mock(return_value=None)
    monkeypatch.setattr(solar, "calculate_adjusted_forecast", calculator)
    assert solar.refresh_adjusted_forecast(Mock(), as_of, "UTC", models=models) == 0
    assert calculator.call_args.kwargs["models"] is models


def test_replay_cli_reads_configured_models(tmp_path, config_data, monkeypatch):
    import solar_model.tools.rebuild_forecasts as replay

    config_data["database"]["path"] = "configured-replay.db"
    config_data["timezone"] = "UTC"
    config_data["forecast"]["model_calibration_parameters"]["solar_intraday"] = {"lookback": 2., "halflife": 0.5, "prior_effective_observations": 0.5}
    path = write_config(tmp_path, config_data)
    database = MagicMock()
    opener = Mock(return_value=database)
    monkeypatch.setattr(replay, "open_database", opener)
    monkeypatch.setattr(replay, "_inferred_bounds", lambda *args: (date(2026, 10, 23), date(2026, 10, 23)))
    frames = pd.DataFrame({"snapshot": pd.to_datetime(["2026-10-23T10:00Z"])})
    monkeypatch.setattr(replay, "load_operational_inputs", Mock(return_value=(frames, pd.DataFrame())))
    calculator = Mock(return_value=None)
    monkeypatch.setattr(replay, "calculate_adjusted_forecast", calculator)
    monkeypatch.setattr("sys.argv", ["replay", "--config", str(path), "--apply"])
    replay.main()
    opener.assert_called_once_with("configured-replay.db")
    assert calculator.call_args.kwargs["models"].solar_intraday == ForecastModelConfig(2., 0.5, 0.5)
    database.close.assert_called_once()
