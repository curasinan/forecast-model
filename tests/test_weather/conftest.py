"""Pin the weather data root to tmp_path for every test in this directory.

``weather.storage.init_weather_db`` calls ``resolve_weather_data_root()``
even when it is handed an explicit db path (it needs the root for the
OneDrive warning and for ``<root>/meta.json``), and the resolver mkdirs
the root plus its six sub-directories. With ``settings.weather_data_root``
empty that root is ``~/quant_data/polymarket_weather`` -- the operator's
live data. The resolver reads the ``settings`` singleton, not the
environment, so ``monkeypatch.setenv("WEATHER_DATA_ROOT", ...)`` would
not reach it; the attribute is patched instead.

Same intent as the CLIMATE_DATA_ROOT + tmp_path guard in
tests/test_climate/test_bridge_window_study.py. Tests that exercise the
default branch on purpose (test_labels_and_license.py) re-blank the
attribute themselves and point ``Path.home`` at their own tmp_path.
The session-wide backstop is tests/test_architecture/test_no_home_writes.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def _weather_root_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "weather_data_root", str(tmp_path / "weather_root"))
