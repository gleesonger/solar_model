"""Locations of packaged, read-only application resources."""
from pathlib import Path


def package_path(path: str | Path) -> Path:
    return Path(__file__).resolve().parent / path


def subprocess_environment() -> dict[str, str]:
    """Keep package imports working for subprocesses launched from a config folder."""
    import os

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parent.parent), environment.get("PYTHONPATH"),
    )))
    return environment
