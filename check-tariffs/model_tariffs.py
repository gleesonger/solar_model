import csv
import json
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TypeAlias
from zoneinfo import ZoneInfo

import pandas as pd


JsonObject: TypeAlias = dict[str, Any]

TIMEZONE: ZoneInfo = ZoneInfo("Europe/Dublin")
INPUT_CSV: Path = Path("minute_data.csv")
OUTPUT_XLSX: Path = Path("tariff_model_results.xlsx")

BATTERY_EFFICIENCY: float = 1.0


def minute_number(value: str) -> int:
    hour, minute = map(int, value.split(":")[:2])
    return hour * 60 + minute


def time_matches(start: str, end: str, minute: int) -> bool:
    start = minute_number(start)
    end = minute_number(end)
    if start == end:
        return True
    if end == 1440:
        return minute >= start
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def rate_matches(rate: JsonObject, local_date: date, minute: int) -> bool:
    days = rate.get("days")
    return (not days or local_date.weekday() in days) and time_matches(rate["start"], rate["end"], minute)


def validate_tariffs(tariffs: list[JsonObject]) -> None:
    for tariff in tariffs:
        for weekday in range(7):
            check_date = date(2026, 1, 5 + weekday)
            for minute in range(1440):
                matches = [rate for rate in tariff["rates"] if rate_matches(rate, check_date, minute)]
                if len(matches) != 1:
                    raise ValueError(
                        f'{tariff["supplier"]} / {tariff["plan"]} does not have exactly one rate '
                        f'at weekday {weekday}, minute {minute}'
                    )


def tariff_rate(tariff: JsonObject, local_date: date, local_time: time) -> JsonObject:
    minute = local_time.hour * 60 + local_time.minute
    return next(rate for rate in tariff["rates"] if rate_matches(rate, local_date, minute))


def selected_plan_rule(plan: list[JsonObject], local_date: date, local_time: time) -> JsonObject:
    minute = local_time.hour * 60 + local_time.minute
    for date_range in plan:
        start_date = datetime.strptime(date_range["start_date"], "%d-%b").date().replace(year=local_date.year)
        end_date = datetime.strptime(date_range["end_date"], "%d-%b").date().replace(year=local_date.year)
        if start_date <= local_date <= end_date:
            for rule in date_range["time_ranges"]:
                if time_matches(rule["start_time"], rule["end_time"], minute):
                    return rule
    raise ValueError(f"No energy-plan rule for {local_date} {local_time}")


def energy_limit(watts: float, period_seconds: float) -> float:
    return watts * period_seconds / 3600


def show_progress(label: str, current: int, total: int) -> None:
    width = 30
    filled = int(width * current / total) if total else width
    bar = "=" * filled + "-" * (width - filled)
    print(f"\r{label} [{bar}] {current}/{total}", end="", flush=True)
    if current == total:
        print()


def run_model(
    tariffs: list[JsonObject],
    hardware: JsonObject,
    plan: list[JsonObject],
    plan_name: str,
    progress_label: str,
) -> tuple[list[list[Any]], dict[tuple[str, str, date], dict[str, float]]]:
    results: list[list[Any]] = []
    daily: defaultdict[tuple[str, str, date], dict[str, float]] = defaultdict(
        lambda: {
            "import_wh": 0,
            "export_wh": 0,
            "import_cost_c": 0,
            "export_revenue_c": 0,
            "standing_charge_c": 0,
        }
    )
    with INPUT_CSV.open(newline="") as source:
        data = list(csv.DictReader(source))

    total_work = len(data) * len(tariffs)
    for tariff_number, tariff in enumerate(tariffs):
        battery_wh = hardware["battery_capacity_wh"] * 0.5
        previous_day = None
        for row_number, row in enumerate(data, start=1):
            timestamp = int(row["end_time_stamp"])
            period_seconds = float(row["period_covered_sec"])
            load_wh = float(row["load_wh"])
            local = datetime.fromtimestamp(timestamp, TIMEZONE)
            rule = selected_plan_rule(plan, local.date(), local.time())
            solar_wh = float(row["solar_pv_wh"]) if rule["model_solar"] else 0.0
            rate = tariff_rate(tariff, local.date(), local.time())
            battery_start_wh = battery_wh
            inverter_limit_wh = energy_limit(hardware["inverter_size_w"], period_seconds)
            inverter_used_wh = 0.0
            solar_to_load_wh = min(solar_wh, load_wh, inverter_limit_wh)
            inverter_used_wh += solar_to_load_wh
            load_remaining_wh = load_wh - solar_to_load_wh
            solar_surplus_wh = solar_wh - solar_to_load_wh
            grid_to_battery_wh = 0
            battery_to_load_wh = 0
            battery_to_grid_wh = 0

            fill = rule["fill_battery_from_grid"]
            empty = rule["empty_battery_to_grid"]
            export = rule["export_surplus_pv"]
            solar_charge_target_wh = hardware["battery_capacity_wh"] * export["min_soc"]

            solar_to_battery_wh = min(
                solar_surplus_wh,
                max(solar_charge_target_wh - battery_wh, 0),
                energy_limit(hardware["battery_max_charge_rate_w"], period_seconds),
                max(inverter_limit_wh - inverter_used_wh, 0),
            )
            battery_wh += solar_to_battery_wh * BATTERY_EFFICIENCY
            solar_surplus_wh -= solar_to_battery_wh
            inverter_used_wh += solar_to_battery_wh

            if fill["enabled"]:
                target_wh = hardware["battery_capacity_wh"] * fill["min_soc"]
                grid_to_battery_wh = min(
                    max(target_wh - battery_wh, 0),
                    energy_limit(hardware["battery_max_charge_rate_w"], period_seconds),
                    max(inverter_limit_wh - inverter_used_wh, 0),
                )
                battery_wh += grid_to_battery_wh * BATTERY_EFFICIENCY
                inverter_used_wh += grid_to_battery_wh

            reserve_wh = (
                hardware["battery_capacity_wh"] * empty["max_soc"]
                if empty["enabled"] and empty["max_soc"] is not None
                else 0
            )
            discharge_limit_wh = min(
                energy_limit(hardware["battery_max_discharge_rate_w"], period_seconds),
                max(inverter_limit_wh - inverter_used_wh, 0),
            )

            # While force-filling from the grid, meet any simultaneous load
            # directly from the grid. Discharging the battery here would
            # create an unnecessary grid -> battery -> load round trip.
            if load_remaining_wh > 0 and not fill["enabled"]:
                battery_to_load_wh = min(
                    load_remaining_wh,
                    max(battery_wh - reserve_wh, 0) * BATTERY_EFFICIENCY,
                    discharge_limit_wh,
                )
                battery_wh -= battery_to_load_wh / BATTERY_EFFICIENCY
                load_remaining_wh -= battery_to_load_wh

            if empty["enabled"]:
                battery_to_grid_wh = min(
                    max(battery_wh - reserve_wh, 0) * BATTERY_EFFICIENCY,
                    max(discharge_limit_wh - battery_to_load_wh, 0),
                )
                battery_wh -= battery_to_grid_wh / BATTERY_EFFICIENCY

            export_wh = min(solar_surplus_wh, max(inverter_limit_wh - inverter_used_wh, 0))
            import_wh = load_remaining_wh + grid_to_battery_wh

            standing_charge_c = tariff["standing_charge_c_per_day"] if previous_day != local.date() else 0
            import_cost_c = import_wh / 1000 * rate["import_c_per_kwh"]
            export_revenue_c = (export_wh + battery_to_grid_wh) / 1000 * rate["export_c_per_kwh"]
            cost_c = import_cost_c - export_revenue_c + standing_charge_c
            key = (tariff["supplier"], tariff["plan"], local.date())

            daily[key]["import_wh"] += import_wh
            daily[key]["export_wh"] += export_wh + battery_to_grid_wh
            daily[key]["import_cost_c"] += import_cost_c
            daily[key]["export_revenue_c"] += export_revenue_c
            daily[key]["standing_charge_c"] += standing_charge_c

            results.append([
                timestamp, local.isoformat(), local.replace(tzinfo=None), tariff["supplier"], tariff["plan"],
                plan_name, period_seconds, load_wh, solar_wh, battery_start_wh,
                solar_to_load_wh, solar_to_battery_wh, grid_to_battery_wh, battery_to_load_wh,
                battery_to_grid_wh, import_wh, export_wh + battery_to_grid_wh,
                battery_wh, rate["import_c_per_kwh"], rate["export_c_per_kwh"],
                import_cost_c / 100, export_revenue_c / 100, standing_charge_c / 100, cost_c / 100,
            ])
            previous_day = local.date()
            if row_number % 100 == 0 or row_number == len(data):
                show_progress(
                    progress_label,
                    tariff_number * len(data) + row_number,
                    total_work,
                )
    return results, daily


def aggregate_hourly(periods: list[list[Any]]) -> list[list[Any]]:
    grouped: dict[tuple[str, str, str, datetime], list[Any]] = {}
    rate_counts: dict[tuple[str, str, str, datetime], int] = {}
    sum_columns = (6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 20, 21, 22, 23)
    rate_columns = (18, 19)

    for row in periods:
        local = datetime.fromisoformat(str(row[1]))
        hour = local.replace(minute=0, second=0, microsecond=0)
        key = (str(row[3]), str(row[4]), str(row[5]), hour)
        if key not in grouped:
            grouped[key] = list(row)
            for column in sum_columns:
                grouped[key][column] = float(row[column])
            for column in rate_columns:
                grouped[key][column] = float(row[column])
            rate_counts[key] = 1
            grouped[key][0] = int(hour.timestamp())
            grouped[key][1] = hour.isoformat()
            grouped[key][2] = hour.replace(tzinfo=None)
        else:
            output = grouped[key]
            for column in sum_columns:
                output[column] += float(row[column])
            for column in rate_columns:
                output[column] += float(row[column])
            output[17] = row[17]
            rate_counts[key] += 1

    hourly = []
    for key, row in sorted(grouped.items(), key=lambda item: item[0]):
        for column in rate_columns:
            row[column] /= rate_counts[key]
        hourly.append(row)
    return hourly


with Path("hardware.json").open() as source:
    hardware = json.load(source)
with Path("tarrifs.json").open(encoding="utf-8-sig") as source:
    tariffs = [tariff for tariff in json.load(source) if tariff["enabled"]]
with Path("energy_plan.json").open() as source:
    energy_plan_config = json.load(source)
    configured_plans = energy_plan_config["plans"]
    energy_plans = {
        plan_name: plan_config["date_ranges"]
        for plan_name, plan_config in configured_plans.items()
        if plan_config["enabled"]
    }

# A tariff may restrict the energy plans tested against it by adding an
# optional "energy_plans" list. Omitting the field means all enabled plans.
unknown_energy_plans = {
    plan_name
    for tariff in tariffs
    for plan_name in tariff.get("energy_plans", [])
    if plan_name not in energy_plans
}
if unknown_energy_plans:
    raise ValueError(f"Unknown energy plan(s) in tariff configuration: {sorted(unknown_energy_plans)}")

validate_tariffs(tariffs)


def write_workbook(
    periods: list[list[Any]],
    daily: dict[tuple[str, str, str, date], dict[str, float]],
) -> None:
    periods = aggregate_hourly(periods)
    hourly_columns = [
        "end_time_stamp", "local_time", "excel_timestamp", "supplier", "tariff_plan", "energy_plan",
        "period_sec", "load_wh", "solar_pv_wh", "battery_start_wh", "solar_to_load_wh",
        "solar_to_battery_wh", "grid_to_battery_wh", "battery_to_load_wh", "battery_to_grid_wh", "grid_import_wh",
        "grid_export_wh", "battery_end_wh", "import_c_per_kwh", "export_c_per_kwh",
        "import_cost_eur", "export_revenue_eur", "standing_charge_eur", "net_cost_eur",
    ]
    hourly = pd.DataFrame(periods, columns=hourly_columns)

    daily_columns = [
        "supplier", "tariff_plan", "energy_plan", "date", "grid_import_kwh", "grid_export_kwh",
        "import_cost_eur", "export_revenue_eur", "standing_charge_eur", "net_cost_eur",
    ]
    daily_rows = []
    for (energy_plan, supplier, tariff_plan, day), values in sorted(daily.items()):
        daily_rows.append([
            supplier, tariff_plan, energy_plan, day.isoformat(), values["import_wh"] / 1000,
            values["export_wh"] / 1000, values["import_cost_c"] / 100,
            values["export_revenue_c"] / 100, values["standing_charge_c"] / 100,
            (values["import_cost_c"] - values["export_revenue_c"] + values["standing_charge_c"]) / 100,
        ])
    daily_frame = pd.DataFrame(daily_rows, columns=daily_columns)

    summary_columns = [
        "supplier", "tariff_plan", "energy_plan", "grid_import_kwh", "grid_export_kwh",
        "import_cost_eur", "export_revenue_eur", "standing_charge_eur", "net_cost_eur",
    ]
    summary: defaultdict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for (energy_plan, supplier, tariff_plan, _), values in daily.items():
        item = summary[(supplier, tariff_plan, energy_plan)]
        item[0] += values["import_wh"] / 1000
        item[1] += values["export_wh"] / 1000
        item[2] += values["import_cost_c"]
        item[3] += values["export_revenue_c"]
        item[4] += values["standing_charge_c"]

    summary_rows = []
    for (supplier, tariff_plan, energy_plan), values in sorted(summary.items()):
        summary_rows.append([
            supplier, tariff_plan, energy_plan, *values[:2], values[2] / 100,
            values[3] / 100, values[4] / 100,
            (values[2] - values[3] + values[4]) / 100,
        ])
    summary_frame = pd.DataFrame(summary_rows, columns=summary_columns)

    with pd.ExcelWriter(OUTPUT_XLSX, engine="xlsxwriter") as writer:
        hourly.to_excel(writer, sheet_name="hourly", index=False)
        daily_frame.to_excel(writer, sheet_name="daily_summary", index=False)
        summary_frame.to_excel(writer, sheet_name="summary", index=False)


if OUTPUT_XLSX.exists():
    OUTPUT_XLSX.unlink()

all_periods: list[list[Any]] = []
all_daily: dict[tuple[str, str, str, date], dict[str, float]] = {}
tariff_count = len(tariffs)
for tariff_number, tariff in enumerate(tariffs, start=1):
    paired_plans = [
        (plan_name, plan) for plan_name, plan in energy_plans.items()
        if not tariff.get("energy_plans") or plan_name in tariff["energy_plans"]
    ]
    for plan_number, (plan_name, plan) in enumerate(paired_plans, start=1):
        progress_label = f"Tariff {tariff_number}/{tariff_count}, plan {plan_number}/{len(paired_plans)} {plan_name}"
        periods, daily = run_model([tariff], hardware, plan, plan_name, progress_label)
        all_periods.extend(periods)
        all_daily.update({(plan_name, *key): values for key, values in daily.items()})
        print(f"Complete: {tariff['supplier']} / {tariff['plan']} / {plan_name}")

print(f"Writing {OUTPUT_XLSX}")
write_workbook(all_periods, all_daily)
print("Complete: all plans")
