"""Reusable presentation helpers for dashboard tables."""

from nicegui.elements.table import Table


def mark_final_total_row(table: Table) -> Table:
    """Style a table whose final row is a Total or Lifetime summary."""
    return table.classes("summary-total-table")
