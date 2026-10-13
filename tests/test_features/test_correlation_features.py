"""Verification tests for ``features.correlation_features``."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from features import (
    compute_correlation_features,
    compute_correlation_features_batch,
    load_clusters,
)
from features.correlation_features import CORRELATION_FEATURE_KEYS

T_LATEST = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
HOURS_OF_HISTORY = 48
FIRST_TS = T_LATEST - timedelta(hours=HOURS_OF_HISTORY)
LATEST_SNAPSHOT_T = T_LATEST - timedelta(hours=2)  # step back so the 1h lookback resolves

ALPHA_MEMBERS = ("alpha-1", "alpha-2", "alpha-3")
BETA_MEMBERS = ("beta-a", "beta-b")
GAMMA_MEMBERS = ("gamma-solo",)
UNCLUSTERED_MARKET = "unclustered-1"

_TRAJECTORIES: dict[str, tuple[float, float]] = {
    "alpha-1": (0.30, 0.002),
    "alpha-2": (0.50, 0.001),
    "alpha-3": (0.70, -0.004),
    "beta-a": (0.40, 0.003),
    "beta-b": (0.60, 0.0015),
}


# Fixtures


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("correlation_features_db") / "synthetic.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id   TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                best_bid    REAL,
                best_ask    REAL,
                bid_depth   REAL,
                ask_depth   REAL,
                midpoint    REAL,
                spread      REAL,
                last_price  REAL,
                volume_24h  REAL,
                UNIQUE (market_id, timestamp)
            )
            """
        )
        conn.execute("CREATE INDEX idx_snapshots_market_id ON snapshots (market_id)")
        conn.execute("CREATE TABLE markets (id TEXT PRIMARY KEY, end_date TEXT)")

        all_ids = (*ALPHA_MEMBERS, *BETA_MEMBERS, *GAMMA_MEMBERS, UNCLUSTERED_MARKET)
        end_date = (T_LATEST + timedelta(days=30)).isoformat()
        conn.executemany(
            "INSERT INTO markets (id, end_date) VALUES (?, ?)",
            [(mid, end_date) for mid in all_ids],
        )

        rows = []
        for mid, (base, slope) in _TRAJECTORIES.items():
            for h in range(HOURS_OF_HISTORY + 1):
                ts = FIRST_TS + timedelta(hours=h)
                price = base + slope * h
                rows.append((
                    mid, ts.isoformat(),
                    price - 0.01, price + 0.01, 100.0, 90.0,
                    price, 0.02, price, 1000.0,
                ))
        conn.executemany(
            "INSERT INTO snapshots (market_id, timestamp, best_bid, best_ask, "
            "bid_depth, ask_depth, midpoint, spread, last_price, volume_24h) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def cfg(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("correlation_cfg") / "clusters.json"
    path.write_text(
        json.dumps({
            "alpha": list(ALPHA_MEMBERS),
            "beta": list(BETA_MEMBERS),
            "gamma": list(GAMMA_MEMBERS),
        }),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="session")
def clusters(cfg):
    return load_clusters(cfg)


@pytest.fixture(scope="session")
def latest_snapshot_T() -> datetime:
    """Inside the synthetic snapshot window, so momentum is meaningful."""
    return LATEST_SNAPSHOT_T


# load_clusters


def test_load_clusters_returns_dict_of_string_lists(clusters):
    assert isinstance(clusters, dict)
    assert "alpha" in clusters
    assert "beta" in clusters
    assert "gamma" in clusters
    for name, members in clusters.items():
        assert isinstance(name, str)
        assert isinstance(members, list)
        for mid in members:
            assert isinstance(mid, str)


def test_load_clusters_no_duplicate_market_across_clusters(clusters):
    seen: dict[str, str] = {}
    for cname, members in clusters.items():
        for mid in members:
            assert mid not in seen, (
                f"market {mid} is in both '{seen[mid]}' and '{cname}'"
            )
            seen[mid] = cname


# Smoke


def test_smoke_clustered_market_has_full_feature_dict(db, cfg, latest_snapshot_T):
    feats = compute_correlation_features("alpha-1", latest_snapshot_T, db, cfg)
    assert set(feats.keys()) == set(CORRELATION_FEATURE_KEYS)
    assert feats["cluster_id"] == "alpha"
    assert feats["cluster_size"] == 3
    # cluster average price should be a real float in [0,1]
    assert feats["cluster_avg_price"] is not None
    assert 0.0 <= feats["cluster_avg_price"] <= 1.0
    # dispersion is non-negative
    assert feats["cluster_price_dispersion"] is not None
    assert feats["cluster_price_dispersion"] >= 0.0
    # cluster_lead_signal is non-negative (it's max abs)
    if feats["cluster_lead_signal"] is not None:
        assert feats["cluster_lead_signal"] >= 0.0


# Peer-only semantics


def test_peer_only_average_in_two_member_cluster(db, cfg, latest_snapshot_T, clusters):
    a, b = clusters["beta"]  # guaranteed size-2 by the synthetic config

    # Pull each market's current price via the market_features path.
    from features import compute_features_batch
    feats_df = compute_features_batch([a, b], latest_snapshot_T, db).set_index("market_id")
    price_a = feats_df.at[a, "price_now"]
    price_b = feats_df.at[b, "price_now"]
    assert pd.notna(price_a) and pd.notna(price_b)  # guaranteed by the fixture's data

    a_corr = compute_correlation_features(a, latest_snapshot_T, db, cfg)
    b_corr = compute_correlation_features(b, latest_snapshot_T, db, cfg)
    assert a_corr["cluster_avg_price"] == pytest.approx(float(price_b))
    assert b_corr["cluster_avg_price"] == pytest.approx(float(price_a))


def test_two_member_cluster_dispersion_is_zero(db, cfg, latest_snapshot_T, clusters):
    a, _b = clusters["beta"]  # guaranteed size-2 by the synthetic config
    a_corr = compute_correlation_features(a, latest_snapshot_T, db, cfg)
    assert a_corr["cluster_avg_price"] is not None
    assert a_corr["cluster_price_dispersion"] == pytest.approx(0.0)


# Edge cases


def test_market_not_in_any_cluster_returns_nulls(db, cfg, latest_snapshot_T):
    feats = compute_correlation_features(UNCLUSTERED_MARKET, latest_snapshot_T, db, cfg)
    assert feats["market_id"] == UNCLUSTERED_MARKET
    assert feats["cluster_id"] is None
    assert feats["cluster_size"] is None
    for k in (
        "cluster_avg_price", "cluster_price_dispersion",
        "cluster_avg_momentum_1h", "own_vs_cluster_momentum",
        "cluster_lead_signal",
    ):
        assert feats[k] is None, f"{k} should be None, got {feats[k]!r}"


def test_unknown_market_does_not_crash(db, cfg, latest_snapshot_T):
    feats = compute_correlation_features("does_not_exist_99999", latest_snapshot_T, db, cfg)
    assert feats["market_id"] == "does_not_exist_99999"
    assert feats["cluster_id"] is None
    assert feats["cluster_size"] is None


def test_single_member_cluster_yields_nulls_for_peer_aggregates(tmp_path, db, latest_snapshot_T):
    cfg = tmp_path / "single.json"
    cfg.write_text(json.dumps({"loner": ["solo-market-x"]}), encoding="utf-8")
    feats = compute_correlation_features("solo-market-x", latest_snapshot_T, db, cfg)
    assert feats["cluster_id"] == "loner"
    assert feats["cluster_size"] == 1
    # Peer-only aggregates have no peers to aggregate over -> all None.
    for k in (
        "cluster_avg_price", "cluster_price_dispersion",
        "cluster_avg_momentum_1h", "own_vs_cluster_momentum",
        "cluster_lead_signal",
    ):
        assert feats[k] is None, f"{k} should be None for singleton cluster"


# Lead signal


def test_lead_signal_excludes_self(db, cfg, latest_snapshot_T, clusters):
    """cluster_lead_signal must be computed over peers only, not self."""
    alpha = clusters["alpha"]
    from features import compute_features_batch
    feats_df = compute_features_batch(alpha, latest_snapshot_T, db).set_index("market_id")
    abs_mom = feats_df["momentum_1h"].abs()
    assert abs_mom.notna().sum() == 3

    leader = abs_mom.idxmax()
    assert leader == "alpha-3"
    leader_abs = float(abs_mom.loc[leader])

    leader_corr = compute_correlation_features(leader, latest_snapshot_T, db, cfg)
    assert leader_corr["cluster_lead_signal"] is not None
    assert leader_corr["cluster_lead_signal"] < leader_abs


# Batch parity


def test_batch_matches_singles(db, cfg, latest_snapshot_T, clusters):
    alpha = clusters["alpha"]
    sample = alpha[:5]
    batch = compute_correlation_features_batch(sample, latest_snapshot_T, db, cfg)
    assert list(batch["market_id"]) == sample
    for mid in sample:
        single = compute_correlation_features(mid, latest_snapshot_T, db, cfg)
        row = batch[batch["market_id"] == mid].iloc[0]
        for key in CORRELATION_FEATURE_KEYS:
            sval = single[key]
            bval = row[key]
            if isinstance(bval, float) and math.isnan(bval):
                bval = None
            if isinstance(sval, float) and isinstance(bval, float):
                assert math.isclose(sval, bval, rel_tol=1e-9, abs_tol=1e-9), (
                    f"mismatch on {mid}.{key}: single={sval!r}, batch={bval!r}"
                )
            else:
                assert sval == bval, (
                    f"mismatch on {mid}.{key}: single={sval!r}, batch={bval!r}"
                )


def test_empty_input_returns_empty_dataframe(db, cfg, latest_snapshot_T):
    df = compute_correlation_features_batch([], latest_snapshot_T, db, cfg)
    assert df.empty
    assert list(df.columns) == list(CORRELATION_FEATURE_KEYS)
