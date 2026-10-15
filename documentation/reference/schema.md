# Exact schema field reference

Field inventory derived from the reviewed SQLAlchemy metadata in [database.py](../../database.py). This is a static documentation snapshot, not a database inspection or migration. Update it when model declarations change.

Read [storage.md](../implementation/storage.md) for derivations, transaction boundaries, rollup rules, and retrieval. Column SQL types describe storage; the API column below describes SQLAlchemy values.

- `scaled number`: INTEGER storage = physical value multiplied by 10,000; ORM returns float. Units follow field names (costs are money; rates are money/kWh).
- `epoch/ISO`: INTEGER UTC epoch-second storage; ORM returns UTC ISO text. Naive binding assumes Europe/Dublin.
- Device timestamps and values are ordinary strings; they do not use these decorators.
- Hourly numeric columns mirror electrical fields, but store sums or cumulative maxima. ORM conversion removes fixed-point scale; it does not convert sums into means.
- Hybrid `collected_at_local` on electrical/forecast models is not a physical column and is therefore absent here.

## `sigenstor`

Primary key: `collected_at_utc`.

| Field | SQL type | ORM API | Nullable |
| --- | --- | --- | --- |
| `collected_at_utc` | `INTEGER` | epoch/ISO | no |
| `plant_grid_power_kw` | `INTEGER` | scaled number | yes |
| `plant_pv_power_kw` | `INTEGER` | scaled number | yes |
| `plant_battery_power_kw` | `INTEGER` | scaled number | yes |
| `plant_battery_soc_percent` | `INTEGER` | scaled number | yes |
| `plant_battery_available_discharge_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_daily_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_pv_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_load_power_kw` | `INTEGER` | scaled number | yes |
| `plant_load_daily_kwh` | `INTEGER` | scaled number | yes |
| `plant_load_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_load_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_load_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_grid_import_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_grid_import_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_grid_export_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_grid_export_total_kwh_period` | `INTEGER` | scaled number | yes |
| `import_tariff_band` | `TEXT` | string | yes |
| `import_rate` | `INTEGER` | scaled number | yes |
| `export_rate` | `INTEGER` | scaled number | yes |
| `grid_import_cost_period` | `INTEGER` | scaled number | yes |
| `grid_export_revenue_period` | `INTEGER` | scaled number | yes |
| `net_cost_period` | `INTEGER` | scaled number | yes |
| `no_solar_battery_import_cost_period` | `INTEGER` | scaled number | yes |
| `plant_battery_charge_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_battery_charge_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_battery_discharge_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_battery_discharge_total_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_battery_avg_cell_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_battery_charge_daily_kwh` | `INTEGER` | scaled number | yes |
| `inverter_battery_charge_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_battery_discharge_daily_kwh` | `INTEGER` | scaled number | yes |
| `inverter_battery_discharge_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv1_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv1_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv2_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv2_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv3_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv3_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv4_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv4_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv1_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv1_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv2_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv2_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv3_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv3_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv4_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv4_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_grid_frequency_hz` | `INTEGER` | scaled number | yes |
| `inverter_phase_a_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_b_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_c_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_a_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_phase_b_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_phase_c_current_amps` | `INTEGER` | scaled number | yes |

## `sigenstor_hourly`

Primary key: `hour_start_utc`, `import_tariff_band`.

| Field | SQL type | ORM API | Nullable |
| --- | --- | --- | --- |
| `hour_start_utc` | `INTEGER` | epoch/ISO | no |
| `import_tariff_band` | `TEXT` | string | no |
| `sample_count` | `INTEGER` | integer | no |
| `grid_power_sample_count` | `INTEGER` | integer (non-null grid readings) | yes |
| `plant_grid_import_power_kw` | `INTEGER` | scaled positive-power sum | yes |
| `plant_grid_export_power_kw` | `INTEGER` | scaled negative-power magnitude sum | yes |
| `plant_pv_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `plant_battery_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `plant_battery_soc_percent_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `plant_battery_available_discharge_kwh_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `plant_load_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `import_rate_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `export_rate_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_battery_avg_cell_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv1_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv1_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv2_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv2_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv3_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv3_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv4_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv4_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv1_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv2_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv3_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_pv4_power_kw_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_grid_frequency_hz_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_a_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_b_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_c_voltage_volts_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_a_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_b_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `inverter_phase_c_current_amps_sample_count` | `INTEGER` | integer (non-null field readings) | yes |
| `plant_grid_power_kw` | `INTEGER` | scaled number | yes |
| `plant_pv_power_kw` | `INTEGER` | scaled number | yes |
| `plant_battery_power_kw` | `INTEGER` | scaled number | yes |
| `plant_battery_soc_percent` | `INTEGER` | scaled number | yes |
| `plant_battery_available_discharge_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_daily_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_pv_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_pv_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_load_power_kw` | `INTEGER` | scaled number | yes |
| `plant_load_daily_kwh` | `INTEGER` | scaled number | yes |
| `plant_load_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_load_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_load_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_grid_import_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_grid_import_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_grid_export_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_grid_export_total_kwh_period` | `INTEGER` | scaled number | yes |
| `import_rate` | `INTEGER` | scaled number | yes |
| `export_rate` | `INTEGER` | scaled number | yes |
| `grid_import_cost_period` | `INTEGER` | scaled number | yes |
| `grid_export_revenue_period` | `INTEGER` | scaled number | yes |
| `net_cost_period` | `INTEGER` | scaled number | yes |
| `no_solar_battery_import_cost_period` | `INTEGER` | scaled number | yes |
| `plant_battery_charge_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_battery_charge_total_kwh_period` | `INTEGER` | scaled number | yes |
| `plant_battery_discharge_total_kwh` | `INTEGER` | scaled number | yes |
| `plant_battery_discharge_total_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_battery_avg_cell_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_battery_charge_daily_kwh` | `INTEGER` | scaled number | yes |
| `inverter_battery_charge_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_battery_discharge_daily_kwh` | `INTEGER` | scaled number | yes |
| `inverter_battery_discharge_daily_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv1_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv1_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv2_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv2_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv3_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv3_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv4_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_pv4_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_pv1_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv1_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv2_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv2_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv3_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv3_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_pv4_power_kw` | `INTEGER` | scaled number | yes |
| `inverter_pv4_energy_kwh_period` | `INTEGER` | scaled number | yes |
| `inverter_grid_frequency_hz` | `INTEGER` | scaled number | yes |
| `inverter_phase_a_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_b_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_c_voltage_volts` | `INTEGER` | scaled number | yes |
| `inverter_phase_a_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_phase_b_current_amps` | `INTEGER` | scaled number | yes |
| `inverter_phase_c_current_amps` | `INTEGER` | scaled number | yes |

## `forecast_solar`

Primary key: `collected_at_utc`, `forecast_time`.

| Field | SQL type | ORM API | Nullable |
| --- | --- | --- | --- |
| `collected_at_utc` | `INTEGER` | epoch/ISO | no |
| `forecast_time` | `INTEGER` | epoch/ISO | no |
| `panel_1_raw_watts` | `INTEGER` | scaled number | yes |
| `panel_1_raw_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_1_raw_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_1_adj_watts` | `INTEGER` | scaled number | yes |
| `panel_1_adj_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_1_adj_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_2_raw_watts` | `INTEGER` | scaled number | yes |
| `panel_2_raw_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_2_raw_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_2_adj_watts` | `INTEGER` | scaled number | yes |
| `panel_2_adj_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_2_adj_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_3_raw_watts` | `INTEGER` | scaled number | yes |
| `panel_3_raw_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_3_raw_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_3_adj_watts` | `INTEGER` | scaled number | yes |
| `panel_3_adj_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_3_adj_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_4_raw_watts` | `INTEGER` | scaled number | yes |
| `panel_4_raw_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_4_raw_watt_hours_day` | `INTEGER` | scaled number | yes |
| `panel_4_adj_watts` | `INTEGER` | scaled number | yes |
| `panel_4_adj_watt_hours` | `INTEGER` | scaled number | yes |
| `panel_4_adj_watt_hours_day` | `INTEGER` | scaled number | yes |

Declared indexes:
- `idx_forecast_time`: `forecast_time`

## `sigenstor_devices`

Primary key: `id`.

| Field | SQL type | ORM API | Nullable |
| --- | --- | --- | --- |
| `id` | `INTEGER` | integer | no |
| `collected_at_utc` | `VARCHAR` | string | no |
| `collected_at_local` | `VARCHAR` | string | no |
| `variable_name` | `VARCHAR` | string | yes |
| `variable` | `VARCHAR` | string | no |
| `value` | `VARCHAR` | string | no |
| `unit` | `VARCHAR` | string | no |

Declared indexes:
- `idx_sigenstor_devices_variable_id`: `variable`, `id`

## `database_metadata`

Primary key: `key`.

| Field | SQL type | ORM API | Nullable |
| --- | --- | --- | --- |
| `key` | `VARCHAR` | string | no |
| `value` | `VARCHAR` | string | no |

## Additional contracts

- Supported forecast panels and PV strings are fixed at IDs 1-4 in model declarations; configuration selects the active subset.
- No inter-table foreign keys are declared. Consumers relate records by time, panel/string mapping, or device variable identity.
- The metadata table is declared, but the current opener does not write a database-version record.
- It records `hourly_rollup_initialized_v2 = "1"` atomically with completed initial hourly creation/recovery; this readiness marker is not a schema-version record.
- Column inventory is generated from `Base.metadata.tables`; it describes the current declared schema, not every possible legacy column remaining in a user database.
