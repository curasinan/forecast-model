"""Label finalization guards + Open-Meteo license gate."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

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
    # Yesterday may or may not be finalized depending on UTC hour; usable needs 72h
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


def test_weather_data_root_not_none():
    root = resolve_weather_data_root()
    assert root.exists()
    # warn helper on fake onedrive path
    from pathlib import Path

    msg = warn_if_onedrive(Path("C:/Users/x/OneDrive/Desktop/data"))
    assert msg is not None
