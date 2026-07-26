"""init_weather_db is an idempotent migration for the new history tables."""

from __future__ import annotations

import sqlite3

from weather.storage import init_weather_db

NEW_OBJECTS = {"forecast_history_daily", "obs_daily", "idx_fhd_join"}


def _names(db) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
    finally:
        conn.close()


def test_double_init_idempotent(tmp_path):
    db = tmp_path / "weather.db"
    init_weather_db(db)
    init_weather_db(db)
    assert NEW_OBJECTS <= _names(db)


def test_pre_change_db_gains_new_tables(tmp_path):
    db = tmp_path / "weather.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE observations (
            station_icao TEXT NOT NULL, valid_utc TEXT NOT NULL,
            temp_f REAL, temp_c REAL, source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (station_icao, valid_utc, source)
        )
        """
    )
    conn.execute(
        "INSERT INTO observations VALUES ('KLGA', '2024-01-01T00:00:00+0000', 40.0, 4.4, "
        "'iem_asos', '2024-01-01T01:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    init_weather_db(db)
    assert NEW_OBJECTS <= _names(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    finally:
        conn.close()
