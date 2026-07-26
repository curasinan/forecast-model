"""EMOS param persistence round-trips. Synthetic only, tmp_path roots."""

from __future__ import annotations

import pytest

from weather.emos import EMOSParams, predict_emos
from weather.model_store import (
    load_all_emos_params,
    load_emos_params,
    model_filename,
    save_emos_params,
)


@pytest.fixture()
def params() -> EMOSParams:
    return EMOSParams(1.25, 0.98, 1.5, 0.6, station_icao="KTST", lead_day=2, metric="low")


META = {
    "n_samples": 40,
    "train_start": "2024-05-01",
    "train_end": "2024-06-09",
    "mean_crps_insample": 1.234,
    "feature_set": "xmodel_prevruns_v1",
    "label_source": "iem_asos",
}


def test_round_trip(tmp_path, params):
    path = save_emos_params(params, metadata=META, root=tmp_path)
    assert path.name == model_filename("KTST", "low", 2) == "KTST_low_lead2.json"
    loaded, meta = load_emos_params(path)
    assert loaded == params
    assert meta == META
    # Predictions identical through the round trip.
    orig = predict_emos(params, 70.0, 9.0)
    back = predict_emos(loaded, 70.0, 9.0)
    assert back.mu == pytest.approx(orig.mu)
    assert back.sigma == pytest.approx(orig.sigma)


def test_overwrite_atomic(tmp_path, params):
    save_emos_params(params, metadata=META, root=tmp_path)
    updated = EMOSParams(2.0, 1.01, 1.1, 0.4, station_icao="KTST", lead_day=2, metric="low")
    path = save_emos_params(updated, metadata=META, root=tmp_path)
    loaded, _ = load_emos_params(path)
    assert loaded.a == 2.0
    assert not list(path.parent.glob("*.tmp"))  # no torn temp files left


def test_load_all_discovers_and_skips_corrupt(tmp_path, params):
    save_emos_params(params, metadata=META, root=tmp_path)
    other = EMOSParams(0.0, 1.0, 2.0, 0.5, station_icao="KLGA", lead_day=0, metric="high")
    save_emos_params(other, metadata=META, root=tmp_path)
    corrupt = tmp_path / "models" / "emos" / "broken.json"
    corrupt.write_text("{not json", encoding="utf-8")
    out = load_all_emos_params(tmp_path)
    assert set(out) == {("KTST", "low", 2), ("KLGA", "high", 0)}
    assert out[("KTST", "low", 2)][0] == params
