"""Weather data root — keep Parquet/SQLite off OneDrive sync."""

from __future__ import annotations

import os
from pathlib import Path

from src.config import settings


def resolve_weather_data_root(override: str | Path | None = None) -> Path:
    """Return the local weather data directory.

    Priority:
      1. explicit override
      2. settings.weather_data_root / WEATHER_DATA_ROOT
      3. %LOCALAPPDATA%/polymarket_weather (Windows)
      4. ~/.local/share/polymarket_weather
    """
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif settings.weather_data_root.strip():
        root = Path(settings.weather_data_root).expanduser().resolve()
    else:
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            root = Path(local_app) / "polymarket_weather"
        else:
            root = Path.home() / ".local" / "share" / "polymarket_weather"

    root.mkdir(parents=True, exist_ok=True)
    for sub in ("forecasts", "obs", "labels", "market_snapshots", "models", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


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
