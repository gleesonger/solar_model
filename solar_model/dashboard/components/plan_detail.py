"""Shared saved/live plan table rendering and spreadsheet export."""
from __future__ import annotations

from html import escape
from typing import Any

import pandas as pd
from nicegui import ui

from solar_model.dashboard.components.download_dialog import excel_bytes
from solar_model.dashboard.components.tables import mark_final_total_row
from solar_model.planning.plan_report import advanced_detail_totals, detail_column, detail_columns, detail_decimal_places, detail_rows, detail_totals, table_detail_frame
from solar_model.planning.types import OperatingMode


# Source field, export variable, spreadsheet heading. Independent of UI table labels.
ADVANCED_EXPORT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("starts_at", "starts_at_local", "Start (local)"),
    ("ends_at", "ends_at_local", "End (local)"),
    ("duration_mins", "duration_mins", "Duration (min)"),
    ("starts_at_utc", "starts_at_utc", "Start (UTC)"),
    ("ends_at_utc", "ends_at_utc", "End (UTC)"),
    ("plan_action", "planned_action", "Planned action"),
    ("tariff_name", "import_tariff_name", "Import tariff"),
    ("import_rate", "import_rate_eur_per_kwh", "Import rate (€/kWh)"),
    ("export_rate", "export_rate_eur_per_kwh", "Export rate (€/kWh)"),
    ("forecast_solar_kw", "forecast_solar_kw", "Solar forecast (kW)"),
    ("actual_solar_kw", "actual_solar_kw", "Solar actual (kW)"),
    ("actual_solar_coverage_mins", "actual_solar_coverage_mins", "Solar actual coverage (min)"),
    ("forecast_house_load_kw", "forecast_house_load_kw", "House load forecast (kW)"),
    ("actual_house_load_kw", "actual_house_load_kw", "House load actual (kW)"),
    ("actual_house_load_coverage_mins", "actual_house_load_coverage_mins", "House load actual coverage (min)"),
    ("forecast_grid_import_kw", "forecast_grid_import_kw", "Grid import forecast (kW)"),
    ("actual_grid_import_kw", "actual_grid_import_kw", "Grid import actual (kW)"),
    ("actual_grid_import_coverage_mins", "actual_grid_import_coverage_mins", "Grid import actual coverage (min)"),
    ("forecast_grid_export_kw", "forecast_grid_export_kw", "Grid export forecast (kW)"),
    ("actual_grid_export_kw", "actual_grid_export_kw", "Grid export actual (kW)"),
    ("actual_grid_export_coverage_mins", "actual_grid_export_coverage_mins", "Grid export actual coverage (min)"),
    ("battery_end_kwh", "forecast_battery_end_kwh", "Battery forecast end (kWh)"),
    ("actual_battery_latest_kwh", "actual_battery_latest_kwh", "Battery actual latest (kWh)"),
    ("actual_battery_read_at_utc", "actual_battery_read_at_utc", "Battery actual reading time (UTC)"),
    ("battery_end_soc_percent", "forecast_battery_end_soc_percent", "Battery forecast end SOC (%)"),
    ("battery_charging", "forecast_battery_charging", "Battery forecast charging"),
    ("minimum_battery_kwh", "battery_min_target_kwh", "Battery minimum target (kWh)"),
    ("maximum_battery_kwh", "battery_max_target_kwh", "Battery maximum target (kWh)"),
    ("limit_battery_export_rate_to_grid_kw", "battery_to_grid_limit_kw", "Battery-to-grid limit (kW)"),
    ("solar_kwh", "forecast_solar_kwh", "Solar forecast energy (kWh)"),
    ("load_kwh", "forecast_house_load_kwh", "House load forecast energy (kWh)"),
    ("grid_import_kwh", "forecast_grid_import_kwh", "Grid import forecast energy (kWh)"),
    ("grid_export_kwh", "forecast_grid_export_kwh", "Grid export forecast energy (kWh)"),
    ("import_cost", "forecast_import_cost_eur", "Import cost forecast (€)"),
    ("export_revenue", "forecast_export_revenue_eur", "Export revenue forecast (€)"),
    ("net_cost", "forecast_net_cost_eur", "Net cost forecast (€)"),
    ("solar_to_load_kwh", "forecast_solar_to_house_load_kwh", "Solar to house load (kWh)"),
    ("solar_to_battery_kwh", "forecast_solar_to_battery_kwh", "Solar to battery (kWh)"),
    ("solar_to_grid_kwh", "forecast_solar_to_grid_kwh", "Solar to grid (kWh)"),
    ("grid_to_load_kwh", "forecast_grid_to_house_load_kwh", "Grid to house load (kWh)"),
    ("grid_to_battery_kwh", "forecast_grid_to_battery_kwh", "Grid to battery (kWh)"),
    ("battery_to_load_kwh", "forecast_battery_to_house_load_kwh", "Battery to house load (kWh)"),
    ("battery_to_grid_kwh", "forecast_battery_to_grid_kwh", "Battery to grid (kWh)"),
    ("inverter_ac_to_dc_kwh", "forecast_inverter_ac_to_dc_kwh", "Inverter AC to DC (kWh)"),
    ("inverter_dc_to_ac_kwh", "forecast_inverter_dc_to_ac_kwh", "Inverter DC to AC (kWh)"),
    ("losses_kwh", "forecast_conversion_losses_kwh", "Conversion losses (kWh)"),
    ("solar_clipped_kwh", "forecast_solar_clipped_kwh", "Solar clipping (kWh)"),
    ("cost_score", "forecast_cost_score", "Cost score"),
    ("protection_score", "forecast_protection_score", "Reserve protection score"),
    ("limit_score", "forecast_battery_limit_score", "Battery limit penalty score"),
    ("terminal_score", "forecast_terminal_score", "Terminal credit score"),
    ("total_score", "forecast_total_score", "Total score"),
)


def render_detail_table(frame: pd.DataFrame, timezone_name: str, mode: OperatingMode, view: str) -> list[dict[str, Any]]:
    columns = detail_columns(frame, compact=view == "Compact")
    display = table_detail_frame(frame)
    table = ui.table(columns=columns, rows=detail_rows(display, timezone_name, mode), row_key="period", pagination=None).props("dense flat bordered").classes("w-full max-w-full")
    if view == "Advanced":
        table.classes("plan-detail-table--advanced")
    total_frame = frame.assign(t=display.t)
    footer_rows = advanced_detail_totals(total_frame) if view == "Advanced" else [detail_totals(total_frame)]
    footer_html = []
    for index, totals in enumerate(footer_rows):
        for column in columns:
            name = column["name"]
            if totals.get(name) is not None and detail_decimal_places(name) is not None:
                totals[name] = f"{totals[name]:.{detail_decimal_places(name)}f}"
        row_class = ' class="plan-subtotal-row"' if index < len(footer_rows) - 1 else ""
        footer_html.append("<q-tr" + row_class + ">" + "".join(
            f'<q-td class="text-{column["align"]}">{escape(str(totals[column["name"]])) if totals.get(column["name"]) is not None else ""}</q-td>'
            for column in columns
        ) + "</q-tr>")
    table.add_slot("bottom-row", "".join(footer_html))
    mark_final_total_row(table).classes("plan-detail-table")
    ui.element("div").classes("h-16")
    return columns


def detail_excel(
    frame: pd.DataFrame,
    timezone_name: str,
    mode: OperatingMode,
    view: str,
    *,
    report_basis: str | None = None,
    actuals: pd.DataFrame | None = None,
    metadata: dict[str, Any] | None = None,
) -> bytes:
    columns = [column for column in detail_columns(frame, compact=view == "Compact") if detail_column(column["name"]).export]
    rows = detail_rows(frame, timezone_name, mode, time_format="%Y-%m-%d %H:%M:%S")
    hours = (frame.ends_at - frame.starts_at).dt.total_seconds() / 3600
    total_frame = frame.assign(t=hours)
    footers = advanced_detail_totals(total_frame) if view == "Advanced" else [detail_totals(total_frame)]
    exported = pd.DataFrame([*rows, *footers])
    measured = interval_actuals(frame, actuals if actuals is not None else pd.DataFrame())
    if view == "Advanced":
        for name in measured.columns:
            exported[name] = [*measured[name].tolist(), *([None] * len(footers))]
        exported["duration_mins"] = [*(hours * 60).tolist(), *[footer["t"] * 60 if footer.get("t") is not None else None for footer in footers]]
        exported = exported.reindex(columns=[source for source, _, _ in ADVANCED_EXPORT_COLUMNS])
        exported.columns = [name for _, name, _ in ADVANCED_EXPORT_COLUMNS]
        exported = exported.rename(columns={name: label for _, name, label in ADVANCED_EXPORT_COLUMNS})
        report_metadata = {"snapshot_id": None, "model": None, "view": view, "solar_scenario": None, "house_load_scenario": None, "report_basis": report_basis}
        report_metadata.update(metadata or {})
        return excel_bytes(exported, sheet_name="Plan detail", metadata=report_metadata)
    exported = exported.reindex(columns=[column["name"] for column in columns])
    exported = exported.rename(columns={column["name"]: column["label"].replace("\n", " ") for column in columns})
    if report_basis is not None:
        exported["Report basis"] = report_basis
    if actuals is not None:
        labels = {"starts_at_utc": "Start (UTC)", "ends_at_utc": "End (UTC)", "forecast_battery_end_kwh": "Forecast Battery end (kWh)", "actual_battery_latest_kwh": "Actual Battery (kWh)", "actual_battery_read_at_utc": "Actual Battery timestamp (UTC)"}
        for metric, label in (("solar", "Solar"), ("house_load", "House Load"), ("grid_import", "Grid Import"), ("grid_export", "Grid Export")):
            labels.update({f"forecast_{metric}_kw": f"Forecast {label} (kW)", f"actual_{metric}_kw": f"Actual {label} (kW)", f"actual_{metric}_coverage_mins": f"Actual {label} coverage (min)"})
        for name in measured.columns:
            exported[labels[name]] = [*measured[name].tolist(), *([None] * len(footers))]
    return excel_bytes(exported, sheet_name="Plan detail")


def interval_actuals(frame: pd.DataFrame, actuals: pd.DataFrame) -> pd.DataFrame:
    """Align measured minute power to plan intervals without filling telemetry gaps."""
    observed = actuals.reindex(columns=["timestamp", "solar", "house_load", "grid_import", "grid_export", "battery"]).copy()
    observed["timestamp"] = pd.to_datetime(observed.timestamp, utc=True)
    observed = observed.sort_values("timestamp")
    records = []
    for row in frame.itertuples():
        start, end = row.starts_at, row.ends_at
        hours = (end - start).total_seconds() / 3600
        window = observed.loc[(observed.timestamp < end) & (observed.timestamp + pd.Timedelta(minutes=1) > start)]
        seconds = ((window.timestamp + pd.Timedelta(minutes=1)).clip(upper=end) - window.timestamp.clip(lower=start)).dt.total_seconds()
        values: dict[str, Any] = {"starts_at_utc": start.isoformat(), "ends_at_utc": end.isoformat()}
        for actual_column, energy_column in (
            ("solar", "solar_kwh"),
            ("house_load", "load_kwh"),
            ("grid_import", "grid_import_kwh"),
            ("grid_export", "grid_export_kwh"),
        ):
            energy = getattr(row, energy_column, None)
            valid = window[actual_column].notna()
            covered = float(seconds.loc[valid].sum())
            values[f"forecast_{actual_column}_kw"] = float(energy) / hours if pd.notna(energy) else None
            values[f"actual_{actual_column}_kw"] = float((window.loc[valid, actual_column] * seconds.loc[valid]).sum()) / covered if covered else None
            values[f"actual_{actual_column}_coverage_mins"] = covered / 60 if covered else None
        battery = observed.loc[(observed.timestamp >= start) & (observed.timestamp <= end) & observed.battery.notna()]
        values["forecast_battery_end_kwh"] = row.battery_end_kwh
        values["actual_battery_latest_kwh"] = float(battery.battery.iloc[-1]) if not battery.empty else None
        values["actual_battery_read_at_utc"] = battery.timestamp.iloc[-1].isoformat() if not battery.empty else None
        records.append(values)
    return pd.DataFrame(records)
