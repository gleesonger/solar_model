from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from forecast.solar_adjustment import (
    PANEL_IDS,
    adjusted_values,
    read_actual_interval_energy,
    rolling_energy_features,
    training_frame,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE sigenstor (collected_at_utc INTEGER, plant_pv_power_kw INTEGER, inverter_pv1_energy_kwh_period INTEGER)")
    connection.execute("CREATE TABLE forecast_solar (collected_at_utc INTEGER, forecast_time INTEGER, panel_1_raw_watts INTEGER)")
    return connection


def test_rolling_energy_requires_complete_window_and_never_reads_future() -> None:
    connection = _connection()
    snapshot = pd.Timestamp("2026-06-01T12:00:00Z")
    # One-minute interval energy is 0.01 kWh, stored as ScaledInteger.
    for minute in range(180):
        timestamp = snapshot - pd.Timedelta(minutes=180 - minute)
        connection.execute("INSERT INTO sigenstor VALUES (?, ?, ?)", (int(timestamp.timestamp()), 0, 100))
    # A huge future value must not affect either feature.
    connection.execute("INSERT INTO sigenstor VALUES (?, ?, ?)", (int((snapshot + pd.Timedelta(minutes=1)).timestamp()), 0, 9_999_000))
    energy = read_actual_interval_energy(connection, snapshot)
    one_hour, three_hours = rolling_energy_features(energy, snapshot)
    assert np.isclose(one_hour, 0.6)
    assert np.isclose(three_hours, 1.8)
    incomplete = rolling_energy_features(energy.iloc[6:], snapshot)
    assert np.isnan(incomplete[1])


def test_training_frame_has_two_month_snapshot_cutoff_and_snapshot_as_of_features() -> None:
    connection = _connection()
    as_of = pd.Timestamp("2026-06-15T12:00:00Z")
    old_snapshot = as_of - pd.Timedelta(days=63)
    snapshot = as_of - pd.Timedelta(days=1)
    target = snapshot + pd.Timedelta(hours=2)
    for timestamp in pd.date_range(snapshot - pd.Timedelta(hours=3), snapshot + pd.Timedelta(hours=3), freq="min", inclusive="left"):
        connection.execute("INSERT INTO sigenstor VALUES (?, ?, ?)", (int(timestamp.timestamp()), 20_000, 100))
    for value in (old_snapshot, snapshot):
        connection.execute("INSERT INTO forecast_solar VALUES (?, ?, ?)", (int(value.timestamp()), int(target.timestamp()), 10_000_000))
    frame = training_frame(connection, as_of)
    assert len(frame) == 1
    assert frame.iloc[0]["snapshot"] == snapshot
    # 60 one-minute values of 0.01 kWh; not values after the snapshot.
    assert np.isclose(frame.iloc[0]["actual_solar_rolling_1h_kwh"], 0.6)


def test_adjusted_energy_is_cumulative_and_daily_total_matches_final_path() -> None:
    snapshot = pd.Timestamp("2026-06-01T00:00:00Z")
    rows = [
        {"forecast_time": (snapshot + pd.Timedelta(hours=offset)).isoformat(), "panel_1_raw_watts": 1000}
        for offset in (8, 9, 10)
    ]
    result = adjusted_values(rows, None, pd.Series(dtype=float), snapshot)
    values = [result[int((snapshot + pd.Timedelta(hours=offset)).timestamp())] for offset in (8, 9, 10)]
    cumulative = [item["panel_1_adj_watt_hours"] for item in values]
    assert cumulative == [1000, 2000, 3000]
    assert {item["panel_1_adj_watt_hours_day"] for item in values} == {3000}
    assert all(item[f"panel_{panel}_adj_watts"] == 0 for item in values for panel in PANEL_IDS if panel != 1)


def test_adjusted_daily_energy_uses_configured_local_day() -> None:
    snapshot = pd.Timestamp("2026-06-01T00:00:00Z")
    rows = [
        {"forecast_time": "2026-06-01T22:00:00Z", "panel_1_raw_watts": 1000},
        {"forecast_time": "2026-06-01T23:00:00Z", "panel_1_raw_watts": 1000},
    ]

    result = adjusted_values(rows, None, pd.Series(dtype=float), snapshot, "Europe/Dublin")

    first = result[int(pd.Timestamp("2026-06-01T22:00:00Z").timestamp())]
    second = result[int(pd.Timestamp("2026-06-01T23:00:00Z").timestamp())]
    assert first["panel_1_adj_watt_hours_day"] == 1000
    assert second["panel_1_adj_watt_hours_day"] == 1000
