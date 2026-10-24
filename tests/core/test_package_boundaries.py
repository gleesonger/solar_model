"""Keep the calculation and storage layers independent of application consumers."""
import ast
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[2] / "solar_model"


@pytest.mark.parametrize("area, forbidden", [
    ("planning", ("solar_model.storage", "solar_model.services", "solar_model.dashboard",
                  "solar_model.integrations", "sqlalchemy", "sqlite3", "requests", "nicegui", "pymodbus")),
    ("storage", ("solar_model.dashboard", "nicegui")),
])
def test_layer_import_boundaries(area, forbidden):
    for path in (PACKAGE / area).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imports = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            assert not any(name == prefix or name.startswith(prefix + ".")
                           for name in imports for prefix in forbidden), path
