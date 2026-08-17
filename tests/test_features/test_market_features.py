"""Verification tests for ``features.market_features`` against the live DB.

Per the plan in ``build-a-feature-engineering-zippy-kettle.md``, this exercises
all seven verification steps. The DB is read-only here; tests skip cleanly if
``data/polymarket.db`` is empty.

Exception: the performance check (section 6) runs against a synthetic DB of
fixed size built in tmp_path, so its workload — and therefore its time
budget — does not drift as the live DB accumulates snapshots.
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

# The perf check must time a FIXED workload. Running over every market in the
# live DB made the test degrade as collector runs accumulated data, failing on
# volume rather than on code. Instead we build a synthetic DB whose schema
# (columns, UNIQUE constraint, index) mirrors collectors/collect_snapshots.py.
#
# Cost is ~linear in markets WITH history (~7ms each on dev hardware, nearly
# independent of snapshots-per-market) while history-less ids are ~free, so
# the pool mixes both — like the live pool, where most markets have no
# snapshots before the frozen evaluation date. 200 dense markets ≈ 1.5-2s.
PERF_N_MARKETS = 1_000  # > IN_CLAUSE_CHUNK (500), so query chunking is exercised
PERF_DENSE_EVERY = 5  # every 5th id has history (200 dense), spread over chunks
PERF_SNAPS_PER_MARKET = 25  # every 4h over (T-96h, T]; every lookback resolves
PERF_T = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
PERF_TIME_BUDGET_S = 5.0  # ~2.5-3x headroom over dev-hardware measurement


def _perf_market_id(i: int) -> str:
    return f"perf-{i:04d}"


def _perf_is_dense(i: int) -> bool:
    return i % PERF_DENSE_EVERY == 0


@pytest.fixture(scope="module")
def perf_db(tmp_path_factory) -> Path:
    """Synthetic fixed-size DB: PERF_N_MARKETS ids, every PERF_DENSE_EVERY-th
    with PERF_SNAPS_PER_MARKET snapshots at/before PERF_T, the rest with no
    history. Values are deterministic functions of the market/snapshot index —
    no randomness, so every run times the exact same workload."""
    path = tmp_path_factory.mktemp("perf") / "perf.db"
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
        conn.execute(
            "CREATE INDEX idx_snapshots_market_id ON snapshots (market_id)"
        )
        conn.execute("CREATE TABLE markets (id TEXT PRIMARY KEY, end_date TEXT)")

        end_date = (PERF_T + timedelta(days=30)).isoformat()
        conn.executemany(
            "INSERT INTO markets (id, end_date) VALUES (?, ?)",
            ((_perf_market_id(i), end_date) for i in range(PERF_N_MARKETS)),
        )

        def snapshot_rows():
            for i in range(PERF_N_MARKETS):
                if not _perf_is_dense(i):
                    continue
                base = 0.05 + 0.9 * ((i * 37) % PERF_N_MARKETS) / PERF_N_MARKETS
                for k in range(PERF_SNAPS_PER_MARKET):
                    ts = (PERF_T - timedelta(hours=4 * k)).isoformat()
                    mid = base + 0.001 * k
                    yield (
                        _perf_market_id(i), ts,
                        mid - 0.01, mid + 0.01,
                        100.0 + i, 80.0 + i,
                        mid, 0.02, mid,
                        1000.0 + i,
                    )

        conn.executemany(
            "INSERT INTO snapshots (market_id, timestamp, best_bid, best_ask, "
            "bid_depth, ask_depth, midpoint, spread, last_price, volume_24h) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            snapshot_rows(),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_batch_under_time_budget_for_fixed_pool(perf_db):
    import time

    ids = [_perf_market_id(i) for i in range(PERF_N_MARKETS)]

    started = time.perf_counter()
    df = compute_features_batch(ids, PERF_T, perf_db)
    elapsed = time.perf_counter() - started

    assert list(df["market_id"]) == ids
    # The budget is only meaningful if the timed run did real work: a
    # mis-built fixture (wrong timestamp format, empty window) would produce
    # all-null rows and a vacuously fast pass.
    dense = df.iloc[[i for i in range(PERF_N_MARKETS) if _perf_is_dense(i)]]
    empty = df.iloc[[i for i in range(PERF_N_MARKETS) if not _perf_is_dense(i)]]
    assert dense["price_now"].notna().all()
    assert dense["momentum_72h"].notna().all()
    assert (dense["snapshot_count"] == PERF_SNAPS_PER_MARKET).all()
    assert empty["price_now"].isna().all()
    assert (empty["snapshot_count"] == 0).all()
    assert elapsed < PERF_TIME_BUDGET_S, (
        f"batch over {PERF_N_MARKETS} markets took {elapsed:.2f}s"
    )


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
