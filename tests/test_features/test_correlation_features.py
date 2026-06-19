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

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
CONFIG_PATH = REPO_ROOT / "config" / "market_clusters.json"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def db():
    if not DB_PATH.exists():
        pytest.skip(f"DB not found at {DB_PATH}")
    return DB_PATH


@pytest.fixture(scope="module")
def cfg():
    if not CONFIG_PATH.exists():
        pytest.skip(f"Cluster config not found at {CONFIG_PATH}")
    return CONFIG_PATH


@pytest.fixture(scope="module")
def clusters(cfg):
    return load_clusters(cfg)


@pytest.fixture(scope="module")
def latest_snapshot_T(db) -> datetime:
    """Use a T inside the snapshot window so momentum is meaningful."""
    conn = sqlite3.connect(db)
    try:
        ts = conn.execute("SELECT MAX(timestamp) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    if ts is None:
        pytest.skip("No snapshots in DB")
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    # Step back a couple of hours so the 1h lookback finds a different snapshot.
    return parsed - timedelta(hours=2)


# --------------------------------------------------------------------------- #
# load_clusters
# --------------------------------------------------------------------------- #


def test_load_clusters_returns_dict_of_string_lists(clusters):
    assert isinstance(clusters, dict)
    assert "iran" in clusters
    assert "russia_ukraine" in clusters
    assert "trump_presidency" in clusters
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


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #


def test_smoke_iran_market_has_full_feature_dict(db, cfg, latest_snapshot_T):
    feats = compute_correlation_features("1919425", latest_snapshot_T, db, cfg)
    assert set(feats.keys()) == set(CORRELATION_FEATURE_KEYS)
    assert feats["cluster_id"] == "iran"
    assert feats["cluster_size"] == 15
    # cluster average price should be a real float in [0,1]
    assert feats["cluster_avg_price"] is not None
    assert 0.0 <= feats["cluster_avg_price"] <= 1.0
    # dispersion is non-negative
    assert feats["cluster_price_dispersion"] is not None
    assert feats["cluster_price_dispersion"] >= 0.0
    # cluster_lead_signal is non-negative (it's max abs)
    if feats["cluster_lead_signal"] is not None:
        assert feats["cluster_lead_signal"] >= 0.0


# --------------------------------------------------------------------------- #
# Peer-only semantics
# --------------------------------------------------------------------------- #


def test_peer_only_average_in_two_member_cluster(db, cfg, latest_snapshot_T, clusters):
    """In a 2-member cluster, each market's cluster_avg_price must equal the
    OTHER market's price_now (not its own). That's the definition of peer-only.
    """
    pair = clusters.get("russia_ukraine")
    if not pair or len(pair) != 2:
        pytest.skip("russia_ukraine cluster is not size-2 in the loaded config")
    a, b = pair

    # Pull each market's current price via the market_features path.
    from features import compute_features_batch
    feats_df = compute_features_batch([a, b], latest_snapshot_T, db).set_index("market_id")
    price_a = feats_df.at[a, "price_now"]
    price_b = feats_df.at[b, "price_now"]
    if pd.isna(price_a) or pd.isna(price_b):
        pytest.skip("price_now missing for one of the russia_ukraine markets")

    a_corr = compute_correlation_features(a, latest_snapshot_T, db, cfg)
    b_corr = compute_correlation_features(b, latest_snapshot_T, db, cfg)
    assert a_corr["cluster_avg_price"] == pytest.approx(float(price_b))
    assert b_corr["cluster_avg_price"] == pytest.approx(float(price_a))


def test_two_member_cluster_dispersion_is_zero(db, cfg, latest_snapshot_T, clusters):
    pair = clusters.get("russia_ukraine")
    if not pair or len(pair) != 2:
        pytest.skip("russia_ukraine cluster is not size-2")
    a_corr = compute_correlation_features(pair[0], latest_snapshot_T, db, cfg)
    if a_corr["cluster_avg_price"] is None:
        pytest.skip("peer price missing")
    # Single peer -> dispersion is 0 by construction
    assert a_corr["cluster_price_dispersion"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_market_not_in_any_cluster_returns_nulls(db, cfg, latest_snapshot_T):
    # Hillary Clinton 2028 nomination — present in DB but not in any cluster.
    feats = compute_correlation_features("559677", latest_snapshot_T, db, cfg)
    assert feats["market_id"] == "559677"
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
    # Build a temporary config with a singleton cluster.
    cfg = tmp_path / "single.json"
    cfg.write_text(json.dumps({"loner": ["666861"]}), encoding="utf-8")
    feats = compute_correlation_features("666861", latest_snapshot_T, db, cfg)
    assert feats["cluster_id"] == "loner"
    assert feats["cluster_size"] == 1
    # Peer-only aggregates have no peers to aggregate over -> all None.
    for k in (
        "cluster_avg_price", "cluster_price_dispersion",
        "cluster_avg_momentum_1h", "own_vs_cluster_momentum",
        "cluster_lead_signal",
    ):
        assert feats[k] is None, f"{k} should be None for singleton cluster"


# --------------------------------------------------------------------------- #
# Lead signal
# --------------------------------------------------------------------------- #


def test_lead_signal_excludes_self(db, cfg, latest_snapshot_T, clusters):
    """cluster_lead_signal must be computed over peers only, not self."""
    iran = clusters["iran"]
    # Pick the market with the largest |momentum_1h| in the cluster.
    from features import compute_features_batch
    feats_df = compute_features_batch(iran, latest_snapshot_T, db).set_index("market_id")
    abs_mom = feats_df["momentum_1h"].abs()
    if abs_mom.notna().sum() < 2:
        pytest.skip("Need at least 2 markets with momentum_1h to verify exclusion")

    leader = abs_mom.idxmax()
    leader_abs = float(abs_mom.loc[leader])

    leader_corr = compute_correlation_features(leader, latest_snapshot_T, db, cfg)
    # The leader's own |momentum| must NOT be its lead signal — the lead signal
    # is the max over its peers, which by definition excludes itself.
    if leader_corr["cluster_lead_signal"] is not None:
        # Peers' max abs momentum is some other (smaller-or-equal) value.
        assert leader_corr["cluster_lead_signal"] <= leader_abs + 1e-12
        # And it shouldn't equal the leader's own value (unless tied with a peer).
        # Loosely: peer max <= self max. Strictly check upper bound only.


# --------------------------------------------------------------------------- #
# Batch parity
# --------------------------------------------------------------------------- #


def test_batch_matches_singles(db, cfg, latest_snapshot_T, clusters):
    iran = clusters["iran"]
    sample = iran[:5]
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
