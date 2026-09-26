from __future__ import annotations

from pathlib import Path

from config import load_config


def test_load_config_parses_minimum_battery_backup_rules_and_ignores_comment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
timezone: Europe/Dublin
logging: {level: INFO}
database: {path: solar.db}
forecast:
  interval_seconds: 3600
  timeout_seconds: 10
  endpoint: https://api.forecast.solar/estimate
  arrays: [{panel_id: 1, name: East, latitude: 0, longitude: 0, declination: 0, azimuth: 0, peak_kw: 1}]
dashboard: {host: 127.0.0.1, port: 8090, actuals_to_forecast: {pv1: 1}}
additional_device_info: {charge_efficiency: 0.95, discharge_efficiency: 0.95}
energy_plan:
  num_projection_hours: 24
  projection_resolution_mins: 15
  load_forecast_historical_lookback_window_days: 42
  load_forecast_historical_lookback_halflife_days: 21
  optimisation_weights:
    maximise_profit: 0.9
    maximise_battery: 0.1
  limit_battery_export_rate_to_grid_kw:
    - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
      start_time: "00:00"
      end_time: "24:00"
      rate: 5.5
  minimum_battery_kwh:
    comment: Retain this much energy for an outage.
    rules:
      - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
        start_time: "00:00"
        end_time: "24:00"
        rate: 4.5
  maximum_battery_kwh:
    - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
      start_time: "00:00"
      end_time: "24:00"
      rate: 18
tariffs:
  periods:
    - effective_from: "2000-01-01"
      import:
        - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
          name: day
          start_time: "00:00"
          end_time: "24:00"
          rate: 0.4
      export:
        - days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
          start_time: "00:00"
          end_time: "24:00"
          rate: 0.2
actuals:
  data_retrival_schedule: {live_update_interval_seconds: 1, full_updated_interval_seconds: 60}
  modbus: {host: localhost, port: 502, timeout_seconds: 5, default_device_id: 1}
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.energy_plan.minimum_battery_kwh[0].rate == 4.5
