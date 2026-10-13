"""Pin the weather data root to tmp_path for every test in this directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import settings


@pytest.fixture(autouse=True)
def _weather_root_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "weather_data_root", str(tmp_path / "weather_root"))
