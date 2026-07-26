"""Parquet + SQLite helpers for weather datasets."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from weather.paths import resolve_weather_data_root, warn_if_onedrive

FEATURE_SCHEMA_VERSION = "weather_v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def weather_db_path(root: Path | None = None) -> Path:
    root = root or resolve_weather_data_root()
    return root / "weather.db"


def init_weather_db(db_path: Path | str | None = None) -> Path:
    root = resolve_weather_data_root()
    warning = warn_if_onedrive(root)
    path = Path(db_path) if db_path else weather_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_markets (
                market_id TEXT PRIMARY KEY,
                event_slug TEXT,
                question TEXT,
                platform TEXT NOT NULL,
                station_icao TEXT NOT NULL,
                valid_local_date TEXT NOT NULL,
                metric TEXT NOT NULL,
                unit TEXT NOT NULL,
                bucket_lo REAL,
                bucket_hi REAL,
                bucket_label TEXT,
                resolution_url TEXT,
                clob_token_ids TEXT,
                parsed_at TEXT NOT NULL,
                raw_description TEXT
            );

            CREATE TABLE IF NOT EXISTS forecast_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_icao TEXT NOT NULL,
                model_name TEXT NOT NULL,
                forecast_init_utc TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                temperature_unit TEXT NOT NULL,
                payload_path TEXT,
                content_hash TEXT,
                UNIQUE (station_icao, model_name, forecast_init_utc, content_hash)
            );

            CREATE TABLE IF NOT EXISTS forecast_daily (
                station_icao TEXT NOT NULL,
                model_name TEXT NOT NULL,
                forecast_init_utc TEXT NOT NULL,
                valid_local_date TEXT NOT NULL,
                lead_hours REAL,
                member_id TEXT,
                temp_max REAL,
                temp_min REAL,
                ensemble_mean_max REAL,
                ensemble_std_max REAL,
                ensemble_mean_min REAL,
                ensemble_std_min REAL,
                fetched_at TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL,
                PRIMARY KEY (
                    station_icao, model_name, forecast_init_utc,
                    valid_local_date, member_id
                )
            );

            CREATE TABLE IF NOT EXISTS observations (
                station_icao TEXT NOT NULL,
                valid_utc TEXT NOT NULL,
                temp_f REAL,
                temp_c REAL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (station_icao, valid_utc, source)
            );

            CREATE TABLE IF NOT EXISTS resolution_labels (
                station_icao TEXT NOT NULL,
                platform TEXT NOT NULL,
                valid_local_date TEXT NOT NULL,
                metric TEXT NOT NULL,
                label_value REAL,
                label_source TEXT NOT NULL,
                label_fetched_at TEXT NOT NULL,
                finalized INTEGER NOT NULL DEFAULT 0,
                usable_for_training INTEGER NOT NULL DEFAULT 0,
                resolution_url TEXT,
                PRIMARY KEY (station_icao, platform, valid_local_date, metric, label_source)
            );

            CREATE TABLE IF NOT EXISTS model_probabilities (
                market_id TEXT NOT NULL,
                decision_ts_utc TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_p REAL NOT NULL,
                mu REAL,
                sigma REAL,
                feature_schema_version TEXT NOT NULL,
                PRIMARY KEY (market_id, decision_ts_utc, model_version)
            );

            CREATE TABLE IF NOT EXISTS forecast_history_daily (
                station_icao TEXT NOT NULL,
                model_name TEXT NOT NULL,
                source_api TEXT NOT NULL DEFAULT 'previous_runs',
                forecast_init_utc TEXT NOT NULL,
                lead_day INTEGER NOT NULL,
                valid_local_date TEXT NOT NULL,
                temp_max REAL,
                temp_min REAL,
                n_hours INTEGER NOT NULL,
                temperature_unit TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL,
                PRIMARY KEY (
                    station_icao, model_name, source_api,
                    valid_local_date, lead_day
                )
            );

            CREATE INDEX IF NOT EXISTS idx_fhd_join
                ON forecast_history_daily (station_icao, lead_day, valid_local_date);

            CREATE TABLE IF NOT EXISTS obs_daily (
                station_icao TEXT NOT NULL,
                valid_local_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'iem_asos',
                tmax_f REAL,
                tmin_f REAL,
                tmax_c REAL,
                tmin_c REAL,
                n_obs INTEGER NOT NULL,
                n_hours_covered INTEGER NOT NULL,
                first_obs_utc TEXT,
                last_obs_utc TEXT,
                finalized INTEGER NOT NULL DEFAULT 0,
                usable_for_training INTEGER NOT NULL DEFAULT 0,
                computed_at TEXT NOT NULL,
                PRIMARY KEY (station_icao, valid_local_date, source)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    meta = root / "meta.json"
    meta.write_text(
        json.dumps(
            {
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "data_root": str(root),
                "onedrive_warning": warning,
                "initialized_at": utc_now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
    except (ImportError, ValueError):
        # Fallback when pyarrow/fastparquet missing.
        csv_path = path.with_suffix(".csv")
        df.to_csv(csv_path, index=False)
        return csv_path
    return path


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def content_hash(payload: Any) -> str:
    import hashlib

    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
