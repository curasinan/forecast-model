"""End-to-end fit over a synthetic DB with known bias. No network."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from weather.history import effective_init_utc
from weather.model_store import load_emos_params
from weather.stations import Station, StationRegistry
from weather.storage import init_weather_db

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fit_emos_history  # noqa: E402

TZ_NY = "America/New_York"
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
OFFSETS = {"ecmwf_ifs025": -1.0, "gfs_seamless": 0.0, "icon_seamless": 1.0}
BIAS = 2.0  # ensemble mean runs 2F warm; a good fit removes it
STAMP = "2024-01-01T00:00:00+00:00"


def _registry() -> StationRegistry:
    return StationRegistry(
        {"KTST": Station("KTST", "Test", "T", 40.0, -73.0, TZ_NY, "F", False, ("global",))},
        {"default": 3.0},
    )


@pytest.fixture()
def seeded_db(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    conn = sqlite3.connect(db)
    d0 = date.fromisoformat("2024-05-01")
    for i in range(40):
        d = (d0 + timedelta(days=i)).isoformat()
        truth = 40.0 + float(i)
        for m in MODELS:
            conn.execute(
                "INSERT INTO forecast_history_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "KTST", m, "previous_runs", effective_init_utc(d, 1, TZ_NY),
                    0, d, truth + BIAS + OFFSETS[m], truth + BIAS + OFFSETS[m] - 10.0,
                    24, "fahrenheit", STAMP, "weather_v1",
                ),
            )
        conn.execute(
            "INSERT INTO obs_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("KTST", d, "iem_asos", truth, truth - 12.0, (truth - 32.0) * 5 / 9,
             (truth - 44.0) * 5 / 9, 24, 24, None, None, 1, 1, STAMP),
        )
    conn.commit()
    conn.close()
    return db


def test_fit_persists_and_removes_bias(seeded_db, tmp_path):
    store = tmp_path / "store"
    results = fit_emos_history.fit_all(
        seeded_db,
        stations=["KTST"],
        metrics=["high"],
        lead_days=[0],
        min_samples=25,
        model_root=store,
        registry=_registry(),
    )
    assert len(results) == 1
    assert results[0]["n"] == 40
    path = store / "models" / "emos" / "KTST_high_lead0.json"
    assert path.exists()
    params, meta = load_emos_params(path)
    assert meta["feature_set"] == "xmodel_prevruns_v1"
    assert meta["label_source"] == "iem_asos"
    assert meta["n_samples"] == 40
    assert meta["train_start"] == "2024-05-01"
    # b near 1; the warm bias is absorbed so predictions land on truth.
    assert 0.8 <= params.b <= 1.2
    errs = [abs(params.a + params.b * (40.0 + i + BIAS) - (40.0 + i)) for i in range(40)]
    assert sum(errs) / len(errs) < 0.75


def test_below_min_samples_never_persists(seeded_db, tmp_path):
    store = tmp_path / "store"
    results = fit_emos_history.fit_all(
        seeded_db,
        stations=["KTST"],
        metrics=["high"],
        lead_days=[3],  # nothing seeded at lead 3
        min_samples=25,
        model_root=store,
        registry=_registry(),
    )
    assert results == []
    assert not (store / "models" / "emos").exists() or not list(
        (store / "models" / "emos").glob("*.json")
    )


def test_dry_run_writes_nothing(seeded_db, tmp_path):
    store = tmp_path / "store"
    results = fit_emos_history.fit_all(
        seeded_db,
        stations=["KTST"],
        metrics=["high"],
        lead_days=[0],
        min_samples=25,
        model_root=store,
        registry=_registry(),
        dry_run=True,
    )
    assert len(results) == 1
    assert results[0]["path"] == "(dry-run)"
    assert not (store / "models" / "emos").exists() or not list(
        (store / "models" / "emos").glob("*.json")
    )


def test_parse_lead_days():
    assert fit_emos_history.parse_lead_days("0-6") == [0, 1, 2, 3, 4, 5, 6]
    assert fit_emos_history.parse_lead_days("0,2,4") == [0, 2, 4]
    assert fit_emos_history.parse_lead_days("3") == [3]
