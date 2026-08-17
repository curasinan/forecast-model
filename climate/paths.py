"""Climate data root -- keep parquet ledgers off OneDrive sync.

Mirrors weather/paths.py: the repo lives under OneDrive, but data files
(parquet/SQLite) must not, because cloud sync mid-write risks corruption.
Default root is %LOCALAPPDATA%/climate_data, overridable via the
CLIMATE_DATA_ROOT environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "CLIMATE_DATA_ROOT"


def data_root(override: str | Path | None = None) -> Path:
    """Return the local climate data directory, creating it if needed.

    Priority:
      1. explicit override argument
      2. CLIMATE_DATA_ROOT environment variable
      3. %LOCALAPPDATA%/climate_data (Windows)
      4. ~/.local/share/climate_data
    """
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif os.environ.get(ENV_VAR, "").strip():
        root = Path(os.environ[ENV_VAR]).expanduser().resolve()
    else:
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            root = Path(local_app) / "climate_data"
        else:
            root = Path.home() / ".local" / "share" / "climate_data"

    root.mkdir(parents=True, exist_ok=True)
    for sub in ("labels", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def warn_if_onedrive(path: Path) -> str | None:
    """Return a warning string if path looks OneDrive-synced, else None."""
    parts = {p.lower() for p in path.parts}
    if "onedrive" in parts:
        return (
            f"Climate data path is under OneDrive ({path}). "
            "Parquet + cloud sync risks corruption; set CLIMATE_DATA_ROOT "
            "to a local non-synced directory."
        )
    return None
