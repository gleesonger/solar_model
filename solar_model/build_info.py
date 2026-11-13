"""Build identity supplied by the image, independent of runtime configuration."""
from __future__ import annotations

import os

GIT_COMMIT: str = os.environ.get("SOLAR_MODEL_GIT_COMMIT", "unknown").strip() or "unknown"
