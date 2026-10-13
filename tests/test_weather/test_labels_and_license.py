"""Label finalization guards + Open-Meteo license gate."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from collectors.weather.collect_labels_wu import (
    is_finalized,
    usable_for_training,
)
from weather.open_meteo import OpenMeteoLicenseError, assert_open_meteo_allowed_for_trading
from weather.paths import resolve_weather_data_root, warn_if_onedrive


def test_finalization_guard():
    today = date.today()
    recent = (today - timedelta(days=1)).isoformat()
    old = (today - timedelta(days=5)).isoformat()
    assert is_finalized(old) is True
    assert usable_for_training(old, finalized=True) is True
    assert usable_for_training(recent, finalized=True) is False
    assert usable_for_training(old, finalized=False) is False


def test_open_meteo_license_blocks_public(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "open_meteo_api_key", "")
    monkeypatch.setattr(settings, "open_meteo_base_url", "")
    monkeypatch.setattr(settings, "open_meteo_allow_noncommercial", False)
    with pytest.raises(OpenMeteoLicenseError):
        assert_open_meteo_allowed_for_trading()


def test_open_meteo_allows_customer_key(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "open_meteo_api_key", "test-key")
    monkeypatch.setattr(settings, "open_meteo_base_url", "")
    monkeypatch.setattr(settings, "open_meteo_allow_noncommercial", False)
    assert_open_meteo_allowed_for_trading()  # no raise


def test_weather_data_root_not_none(tmp_path):
    root = resolve_weather_data_root(str(tmp_path))
    assert root.exists()
    # warn helper on fake onedrive path
    from pathlib import Path

    msg = warn_if_onedrive(Path("C:/Users/x/OneDrive/Desktop/data"))
    assert msg is not None


def test_weather_data_root_default_is_quant_data_home(tmp_path, monkeypatch):
    from pathlib import Path

    from src.config import settings

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(settings, "weather_data_root", "")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    root = resolve_weather_data_root()

    assert root == tmp_path / "quant_data" / "polymarket_weather"
    assert root.exists()


def test_weather_data_root_warns_on_stale_localappdata_data(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    from src.config import settings

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(settings, "weather_data_root", "")
    fake_local_app = tmp_path / "AppData" / "Local"
    old_root = fake_local_app / "polymarket_weather"
    (old_root / "obs").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_app))

    with caplog.at_level("WARNING", logger="weather.paths"):
        new_root = resolve_weather_data_root()

    assert new_root == tmp_path / "quant_data" / "polymarket_weather"
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(old_root) in msg and str(new_root) in msg for msg in messages)
