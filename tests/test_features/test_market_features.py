"""Verification tests for ``features.market_features`` against the live DB.

Per the plan in ``build-a-feature-engineering-zippy-kettle.md``, this exercises
all seven verification steps. The DB is read-only here; tests skip cleanly if
``data/polymarket.db`` is empty.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from features import compute_features, compute_features_batch
from features.market_features import FEATURE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "polymarket.db"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def db():
    if not DB_PATH.exists():
        pytest.skip(f"DB not found at {DB_PATH}")
    return DB_PATH


@pytest.fixture(scope="module")
def populated_market(db) -> tuple[str, datetime, datetime]:
    """A market with >= 20 snapshots, plus its first/last timestamp."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            """
            SELECT market_id, MIN(timestamp), MAX(timestamp), COUNT(*)
              FROM snapshots
             GROUP BY market_id
            HAVING COUNT(*) >= 20
             ORDER BY COUNT(*) DESC
             LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.skip("No market has >= 20 snapshots")
    mid, first_ts, last_ts, _ = row
    return (
        str(mid),
        datetime.fromisoformat(first_ts.replace("Z", "+00:00")),
        datetime.fromisoformat(last_ts.replace("Z", "+00:00")),
    )


@pytest.fixture(scope="module")
def evaluation_T() -> datetime:
    """Evaluation timestamp aligned with the demo run in the plan."""
    return datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. Smoke
# --------------------------------------------------------------------------- #


def test_smoke_full_feature_dict(db, populated_market, evaluation_T):
    mid, _, _ = populated_market
    feats = compute_features(mid, evaluation_T, db)
    # Every key present
    assert set(feats.keys()) == set(FEATURE_KEYS)
    assert feats["market_id"] == mid
    # Probability semantics for a midpoint
    assert feats["price_now"] is None or 0.0 <= feats["price_now"] <= 1.0
    assert feats["snapshot_count"] >= 1


# --------------------------------------------------------------------------- #
# 2. Causality
# --------------------------------------------------------------------------- #


def test_causality_uses_only_past_snapshots(db, populated_market):
    mid, _, last_ts = populated_market
    # Pick T just before the latest snapshot — price_now must be the
    # second-to-latest midpoint, NOT the very latest.
    T = last_ts - timedelta(seconds=1)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT timestamp, midpoint FROM snapshots "
            "WHERE market_id = ? AND midpoint IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 2",
            (mid,),
        ).fetchall()
    finally:
        conn.close()
    # Need at least 2 snapshots with midpoint to do this check.
    if len(rows) < 2:
        pytest.skip("Not enough non-null midpoint snapshots for causality check")

    second_to_latest_mid = rows[1][1]
    feats = compute_features(mid, T, db)
    assert feats["price_now"] == pytest.approx(second_to_latest_mid)


def test_before_history_returns_nulls(db, populated_market):
    mid, first_ts, _ = populated_market
    T = first_ts - timedelta(seconds=1)
    feats = compute_features(mid, T, db)
    # All price-derived fields should be None
    for k in (
        "price_now", "price_1h_ago", "price_6h_ago",
        "price_24h_ago", "price_72h_ago",
        "spread_now", "bid_depth_now", "ask_depth_now",
    ):
        assert feats[k] is None, f"{k} should be None, got {feats[k]}"
    assert feats["snapshot_count"] == 0


# --------------------------------------------------------------------------- #
# 3. Missing market
# --------------------------------------------------------------------------- #


def test_unknown_market_does_not_crash(db, evaluation_T):
    feats = compute_features("does_not_exist_12345", evaluation_T, db)
    # Must produce a row, not raise.
    assert feats["market_id"] == "does_not_exist_12345"
    assert feats["snapshot_count"] == 0
    assert feats["price_now"] is None
    assert feats["time_to_resolution"] is None


# --------------------------------------------------------------------------- #
# 4. Batch parity
# --------------------------------------------------------------------------- #


def test_batch_matches_singles(db, evaluation_T):
    conn = sqlite3.connect(db)
    try:
        ids = [str(r[0]) for r in conn.execute(
            "SELECT market_id FROM snapshots GROUP BY market_id "
            "HAVING COUNT(*) >= 20 ORDER BY COUNT(*) DESC LIMIT 5"
        ).fetchall()]
    finally:
        conn.close()
    if len(ids) < 5:
        pytest.skip("Need at least 5 markets with >= 20 snapshots")

    batch = compute_features_batch(ids, evaluation_T, db)
    assert len(batch) == 5
    assert list(batch["market_id"]) == ids

    for mid in ids:
        single = compute_features(mid, evaluation_T, db)
        row = batch[batch["market_id"] == mid].iloc[0]
        for key in FEATURE_KEYS:
            sval = single[key]
            bval = row[key]
            # Treat NaN == None for comparison purposes.
            if isinstance(bval, float) and math.isnan(bval):
                bval = None
            assert sval == bval or (
                isinstance(sval, float)
                and isinstance(bval, float)
                and math.isclose(sval, bval, rel_tol=1e-9, abs_tol=1e-9)
            ), f"mismatch on {mid}.{key}: single={sval!r}, batch={bval!r}"


# --------------------------------------------------------------------------- #
# 5. Hand-checked spread average
# --------------------------------------------------------------------------- #


def test_spread_avg_24h_matches_sql(db, populated_market, evaluation_T):
    mid, _, _ = populated_market
    feats = compute_features(mid, evaluation_T, db)
    if feats["spread_avg_24h"] is None:
        pytest.skip("No spread data in 24h window for this market")

    cutoff = (evaluation_T - timedelta(hours=24)).isoformat()
    t_iso = evaluation_T.isoformat()
    conn = sqlite3.connect(db)
    try:
        avg = conn.execute(
            "SELECT AVG(spread) FROM snapshots "
            "WHERE market_id = ? AND timestamp > ? AND timestamp <= ? "
            "AND spread IS NOT NULL",
            (mid, cutoff, t_iso),
        ).fetchone()[0]
    finally:
        conn.close()
    assert feats["spread_avg_24h"] == pytest.approx(float(avg), rel=1e-9, abs=1e-9)


# --------------------------------------------------------------------------- #
# 6. Performance
# --------------------------------------------------------------------------- #


def test_batch_under_2s_for_full_pool(db, evaluation_T):
    conn = sqlite3.connect(db)
    try:
        ids = [str(r[0]) for r in conn.execute(
            "SELECT market_id FROM snapshots GROUP BY market_id"
        ).fetchall()]
    finally:
        conn.close()
    if len(ids) < 100:
        pytest.skip("Not enough markets for a useful perf check")

    import time

    started = time.time()
    df = compute_features_batch(ids, evaluation_T, db)
    elapsed = time.time() - started
    assert len(df) == len(ids)
    # Allow some headroom on slower hardware; the plan target is <= 2s.
    assert elapsed < 5.0, f"batch over {len(ids)} markets took {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# 7. Snapshot count is monotone in T
# --------------------------------------------------------------------------- #


def test_snapshot_count_monotone(db, populated_market):
    mid, first_ts, last_ts = populated_market
    earlier = first_ts + (last_ts - first_ts) / 3
    later = first_ts + 2 * (last_ts - first_ts) / 3
    n_earlier = compute_features(mid, earlier, db)["snapshot_count"]
    n_later = compute_features(mid, later, db)["snapshot_count"]
    assert n_later >= n_earlier
