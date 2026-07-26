"""Chunking, cache keying, and upsert guards of the history collector."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from collectors.weather.collect_history_forecasts import (
    chunk_dates,
    fetch_window,
    raw_cache_path,
    store_history_rows,
)
from weather.storage import init_weather_db


def test_chunk_dates_partitions_range():
    chunks = chunk_dates("2024-01-01", "2024-03-31", chunk_days=31)
    assert chunks[0] == ("2024-01-01", "2024-01-31")
    assert chunks[-1][1] == "2024-03-31"
    # Consecutive, no gaps or overlaps at the requested level.
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert pd.Timestamp(next_start) == pd.Timestamp(prev_end) + pd.Timedelta(days=1)


def test_chunk_dates_single_day():
    assert chunk_dates("2024-06-15", "2024-06-15", chunk_days=90) == [
        ("2024-06-15", "2024-06-15")
    ]


def test_chunk_dates_rejects_nonpositive():
    with pytest.raises(ValueError):
        chunk_dates("2024-01-01", "2024-03-31", chunk_days=0)
    with pytest.raises(ValueError):
        chunk_dates("2024-01-01", "2024-03-31", chunk_days=-5)


def test_fetch_window_expands_one_day_each_side():
    assert fetch_window("2024-06-01", "2024-06-30") == ("2024-05-31", "2024-07-01")


def test_raw_cache_path_keyed_by_unit():
    root = Path("root")
    f = raw_cache_path(root, "KLGA", "gfs_seamless", "2024-05-31", "2024-07-01", "fahrenheit")
    c = raw_cache_path(root, "KLGA", "gfs_seamless", "2024-05-31", "2024-07-01", "celsius")
    assert f != c
    assert f.name == "2024-05-31_2024-07-01_fahrenheit.json"
    assert "station=KLGA" in str(f) and "model=gfs_seamless" in str(f)


def _row(**kw) -> dict:
    base = {
        "station_icao": "KLGA",
        "model_name": "gfs_seamless",
        "source_api": "previous_runs",
        "forecast_init_utc": "2024-06-15T04:00:00+00:00",
        "lead_day": 0,
        "valid_local_date": "2024-06-15",
        "temp_max": 80.0,
        "temp_min": 65.0,
        "n_hours": 24,
        "temperature_unit": "fahrenheit",
        "fetched_at": "2024-07-01T00:00:00+00:00",
        "feature_schema_version": "weather_v1",
    }
    base.update(kw)
    return base


def test_store_history_rows_never_degrades_fuller_day(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    store_history_rows(db, pd.DataFrame([_row(temp_max=80.0, n_hours=24)]))
    # A partial stub for the same PK must not clobber the full day...
    store_history_rows(db, pd.DataFrame([_row(temp_max=99.0, n_hours=5)]))
    conn = sqlite3.connect(db)
    try:
        tmax, n_hours = conn.execute(
            "SELECT temp_max, n_hours FROM forecast_history_daily"
        ).fetchone()
        assert (tmax, n_hours) == (80.0, 24)
        # ...but an equally-full re-run updates in place (idempotent heal).
        store_history_rows(db, pd.DataFrame([_row(temp_max=81.0, n_hours=24)]))
        tmax2 = conn.execute("SELECT temp_max FROM forecast_history_daily").fetchone()[0]
        assert tmax2 == 81.0
        assert conn.execute("SELECT COUNT(*) FROM forecast_history_daily").fetchone()[0] == 1
    finally:
        conn.close()
