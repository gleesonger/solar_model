from __future__ import annotations
from datetime import datetime
from pathlib import Path
import pandas as pd

def group_plan(plan: pd.DataFrame, output_timestep_mins: int) -> pd.DataFrame:
    """Aggregate projection rows into clock-aligned output periods."""
    grouped_plan = plan.copy()
    starts = pd.to_datetime(grouped_plan["starts_at"])
    grouped_plan["_output_start"] = starts.dt.floor(
        f"{output_timestep_mins}min", ambiguous=starts.map(lambda value: value.fold == 0).to_numpy(),
        nonexistent="shift_forward",
    )
    sum_columns = {column for column in plan.columns if column.endswith("_kwh") or column in {"import_cost", "export_revenue", "net_cost"}}
    sum_columns -= {"minimum_battery_kwh", "maximum_battery_kwh", "battery_start_kwh", "battery_end_kwh", "limit_battery_export_rate_to_grid_kw"}
    rows = []
    for _, group in grouped_plan.groupby("_output_start", sort=True):
        row = {column: group[column].iloc[0] for column in plan.columns}
        row["starts_at"] = group["starts_at"].min()
        row["ends_at"] = group["ends_at"].max()
        row["minimum_battery_kwh"] = group["minimum_battery_kwh"].max()
        row["maximum_battery_kwh"] = group["maximum_battery_kwh"].min()
        row["limit_battery_export_rate_to_grid_kw"] = group["limit_battery_export_rate_to_grid_kw"].min()
        row["battery_start_kwh"] = group["battery_start_kwh"].iloc[0]
        row["battery_end_kwh"] = group["battery_end_kwh"].iloc[-1]
        for column in sum_columns:
            row[column] = group[column].sum()
        row["tariff_name"] = " / ".join(dict.fromkeys(str(value) for value in group["tariff_name"]))
        for rate_column, volume_column in (("import_rate", "grid_import_kwh"), ("export_rate", "grid_export_kwh")):
            volume = group[volume_column].sum()
            row[rate_column] = (group[rate_column] * group[volume_column]).sum() / volume if volume else group[rate_column].iloc[0]
        rows.append(row)
    return pd.DataFrame(rows, columns=plan.columns)

def write_workbook(plans: dict[str, pd.DataFrame], output: Path, as_of: datetime) -> None:
    """Write one plan worksheet per scenario and a side-by-side summary."""
    summary = pd.DataFrame({name: {"As of": as_of.isoformat(), "Periods": len(plan), "Total import cost": plan["import_cost"].sum(), "Total export revenue": plan["export_revenue"].sum(), "Total net cost": plan["net_cost"].sum()} for name, plan in plans.items()}).rename_axis("item").reset_index()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        workbook = writer.book
        summary_sheet = writer.sheets["Summary"]
        summary_sheet.set_zoom(80)
        money_format = workbook.add_format({"num_format": '€#,##0.00'})
        energy_format = workbook.add_format({"num_format": "0.000"})
        summary_sheet.set_column("A:A", 24)
        summary_sheet.set_column(1, len(plans), 24)
        summary_sheet.set_row(0, None, workbook.add_format({"bold": True, "bg_color": "#D9EAF7"}))
        for row in range(3, 6):
            summary_sheet.set_row(row, None, money_format)
        for name, plan in plans.items():
            plan = plan.copy()
            for column in ("starts_at", "ends_at"):
                plan[column] = pd.to_datetime(plan[column]).dt.tz_localize(None)
            plan.to_excel(writer, sheet_name=name, index=False)
            plan_sheet = writer.sheets[name]
            plan_sheet.set_zoom(80)
            plan_sheet.freeze_panes(1, 0)
            plan_sheet.add_table(0, 0, len(plan), len(plan.columns) - 1, {"columns": [{"header": column} for column in plan.columns], "style": "Table Style Medium 2"})
            for column_number, column in enumerate(plan.columns):
                width = max(len(column), min(24, int(plan[column].astype(str).str.len().max()) + 2))
                plan_sheet.set_column(column_number, column_number, width)
            for column in plan.columns:
                if column.endswith("_kwh"):
                    plan_sheet.set_column(plan.columns.get_loc(column), plan.columns.get_loc(column), 14, energy_format)
                elif column in {"import_rate", "export_rate", "import_cost", "export_revenue", "net_cost"}:
                    plan_sheet.set_column(plan.columns.get_loc(column), plan.columns.get_loc(column), 14, money_format)
            plan_sheet.conditional_format(1, plan.columns.get_loc("net_cost"), len(plan), plan.columns.get_loc("net_cost"), {"type": "3_color_scale", "min_color": "#C6EFCE", "mid_color": "#FFEB9C", "max_color": "#FFC7CE"})
