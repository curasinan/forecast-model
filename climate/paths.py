"""Climate data root -- keep parquet ledgers off OneDrive sync.

Mirrors weather/paths.py: the repo lives under OneDrive, but data files
(parquet/SQLite) must not, because cloud sync mid-write risks corruption.
Default root is ~/quant_data/climate_data, overridable via the
CLIMATE_DATA_ROOT environment variable.

NOT %LOCALAPPDATA% (the old default, migrated 2026-08-25): a packaged
(MSIX) app virtualises AppData per-app, so the same path resolves to two
different stores depending on whether the writer runs inside the packaged
app or outside it (Task Scheduler). The whole climate ledger had ended up
in the container-private copy, invisible to any scheduled job.
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
      3. ~/quant_data/climate_data
    """
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif os.environ.get(ENV_VAR, "").strip():
        root = Path(os.environ[ENV_VAR]).expanduser().resolve()
    else:
        root = Path.home() / "quant_data" / "climate_data"

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
