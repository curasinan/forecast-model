"""Weather data root — keep Parquet/SQLite off OneDrive sync."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def resolve_weather_data_root(override: str | Path | None = None) -> Path:
    """Return the local weather data directory.

    Priority:
      1. explicit override
      2. settings.weather_data_root / WEATHER_DATA_ROOT
      3. ~/quant_data/polymarket_weather (default since the 2026-08-25
         migration; mirrors climate/paths.py and
         src/venues/ibkr/settings.py's resolved_data_root)

    NOT %LOCALAPPDATA% (the old default): a packaged (MSIX) app virtualises
    AppData per-app, so the same path resolves to two different stores
    depending on whether the writer runs inside the packaged app or outside
    it (Task Scheduler) -- see docs/MIGRATION_CHECKPOINT.md section 3. When
    the default branch is used and an old %LOCALAPPDATA%/polymarket_weather
    directory still exists while the new root is empty, this logs a WARNING
    naming both paths. It never auto-moves data.
    """
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif settings.weather_data_root.strip():
        root = Path(settings.weather_data_root).expanduser().resolve()
    else:
        root = Path.home() / "quant_data" / "polymarket_weather"
        _warn_if_stale_localappdata_data(root)

    root.mkdir(parents=True, exist_ok=True)
    for sub in ("forecasts", "obs", "labels", "market_snapshots", "models", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _warn_if_stale_localappdata_data(new_root: Path) -> None:
    """Warn (never auto-move) when old %LOCALAPPDATA% weather data exists
    and the new default directory is still empty -- likely means the data
    was never migrated and this lane, if used again, would start from an
    empty store while the old data sits invisible under AppData."""
    local_app = os.environ.get("LOCALAPPDATA")
    if not local_app:
        return
    old_root = Path(local_app) / "polymarket_weather"
    if not old_root.exists():
        return
    new_is_empty = not new_root.exists() or not any(new_root.iterdir())
    if new_is_empty:
        logger.warning(
            "Old weather data directory found at %s but the new default %s "
            "is empty. Data was NOT moved automatically -- set "
            "WEATHER_DATA_ROOT to point at the old directory, or move the "
            "data yourself, before relying on this lane again.",
            old_root,
            new_root,
        )


def warn_if_onedrive(path: Path) -> str | None:
    """Return a warning string if path looks OneDrive-synced."""
    parts = {p.lower() for p in path.parts}
    if "onedrive" in parts:
        return (
            f"Weather data path is under OneDrive ({path}). "
            "SQLite + cloud sync risks corruption; set WEATHER_DATA_ROOT "
            "to a local non-synced directory."
        )
    return None
