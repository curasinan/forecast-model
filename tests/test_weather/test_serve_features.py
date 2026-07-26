"""Serve-path xmodel_prevruns_v1 features + EMOS feature_set guard."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weather.emos import EMOSParams
from weather.model_store import save_emos_params
from weather.serve_features import (
    build_xmodel_features,
    lead_day_for_market,
    load_emos_for_feature_set,
)
from weather.storage import init_weather_db
from weather.training import FEATURE_SET


def _seed_history(db: Path, *, lead_day: int = 1) -> None:
    conn = __import__("sqlite3").connect(db)
    rows = []
    for model, tmax in (
        ("ecmwf_ifs025", 80.0),
        ("gfs_seamless", 82.0),
        ("icon_seamless", 81.0),
    ):
        # init early enough that lag filter at decision_hour=12 passes
        rows.append(
            (
                "KLGA",
                model,
                "previous_runs",
                "2026-07-25T04:00:00+00:00",
                lead_day,
                "2026-07-27",
                tmax,
                70.0,
                24,
                "fahrenheit",
                "2026-07-26T00:00:00+00:00",
                "weather_v1",
            )
        )
    conn.executemany(
        """
        INSERT INTO forecast_history_daily (
            station_icao, model_name, source_api, forecast_init_utc,
            lead_day, valid_local_date, temp_max, temp_min,
            n_hours, temperature_unit, fetched_at, feature_schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def test_lead_day_for_market():
    now = datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc)  # noon-ish ET
    assert lead_day_for_market("2026-07-27", "America/New_York", now=now) == 2
    assert lead_day_for_market("2026-07-25", "America/New_York", now=now) == 0


def test_build_xmodel_features_from_db(tmp_path: Path):
    db = init_weather_db(tmp_path / "w.db")
    _seed_history(db, lead_day=2)
    feats = build_xmodel_features(
        db,
        station_icao="KLGA",
        valid_local_date="2026-07-27",
        metric="high",
        lead_day=2,
        fetch_if_missing=False,
    )
    assert feats is not None
    assert feats.feature_set == FEATURE_SET
    assert feats.n_models == 3
    assert feats.ens_mean == pytest.approx(81.0)
    # population var of 80,82,81
    assert feats.ens_var == pytest.approx(((1 + 1 + 0) / 3))


def test_build_xmodel_incomplete_returns_none(tmp_path: Path):
    db = init_weather_db(tmp_path / "w.db")
    conn = __import__("sqlite3").connect(db)
    conn.execute(
        """
        INSERT INTO forecast_history_daily (
            station_icao, model_name, source_api, forecast_init_utc,
            lead_day, valid_local_date, temp_max, temp_min,
            n_hours, temperature_unit, fetched_at, feature_schema_version
        ) VALUES ('KLGA','ecmwf_ifs025','previous_runs','2026-07-25T04:00:00+00:00',
                  2,'2026-07-27',80,70,24,'fahrenheit','t','weather_v1')
        """
    )
    conn.commit()
    conn.close()
    assert (
        build_xmodel_features(
            db,
            station_icao="KLGA",
            valid_local_date="2026-07-27",
            metric="high",
            lead_day=2,
            fetch_if_missing=False,
        )
        is None
    )


def test_load_emos_feature_set_guard(tmp_path: Path):
    good = EMOSParams(0.0, 1.0, 2.0, 0.5, "KLGA", 1, "high")
    bad = EMOSParams(0.0, 1.0, 2.0, 0.5, "KLGA", 2, "high")
    save_emos_params(
        good,
        metadata={"feature_set": FEATURE_SET, "n_samples": 40},
        root=tmp_path,
    )
    save_emos_params(
        bad,
        metadata={"feature_set": "ensemble_members_v0", "n_samples": 40},
        root=tmp_path,
    )
    loaded = load_emos_for_feature_set(tmp_path, required_feature_set=FEATURE_SET)
    assert ("KLGA", "high", 1) in loaded
    assert ("KLGA", "high", 2) not in loaded
