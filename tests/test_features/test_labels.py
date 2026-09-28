"""Verification tests for ``features.labels``.

M11 (Task 3.8, test hygiene): these used to read the real (gitignored)
``data/polymarket.db`` and ``config/market_clusters.json``, keyed to
specific real market ids (MegaETH "1731274", Hillary Clinton 2028
"559677", ...) and a hand-picked "dense coverage" timestamp that only held
because of what the live DB happened to contain on the day these tests
were written. Every ``db``/``cfg``/``dense_T`` fixture below is now
synthetic and session-scoped instead: five markets with deterministic,
hourly snapshots spanning the full lookback/horizon window this module
needs, built from the real writer's DDL (``collectors/
collect_snapshots.py`` for ``snapshots``; the ``markets`` columns are the
ones this module and ``features.market_features`` actually read).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from features import (
    build_training_dataset,
    generate_outcome_labels,
    generate_price_movement_labels,
)
from features.labels import (
    _normalize_resolution,
    _pivot_labels_to_wide,
    _feature_columns,
)
from features.market_features import FEATURE_KEYS as MARKET_KEYS
from features.correlation_features import CORRELATION_FEATURE_KEYS as CORR_KEYS

DENSE_T = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
FIRST_TS = DENSE_T - timedelta(hours=100)  # >= 72h (max lookback) before DENSE_T
LAST_TS = DENSE_T + timedelta(hours=30)  # >= 24h (max default horizon) after DENSE_T
SECOND_T = DENSE_T - timedelta(hours=10)  # a second timestamp, still densely covered

MARKET_IDS: tuple[str, ...] = tuple(f"label-market-{i}" for i in range(5))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> Path:
    """Synthetic sqlite DB: ``snapshots`` schema copied verbatim from
    ``collectors/collect_snapshots.py::init_db``; ``markets`` carries the
    columns this module (status, resolution) and
    ``features.market_features`` (end_date) actually read. Five markets,
    each with hourly snapshots from FIRST_TS to LAST_TS -- dense enough
    that every lookback (up to 72h back) and every default horizon (up to
    24h forward) from DENSE_T/SECOND_T resolves to a real value, never a
    null from missing coverage.
    """
    path = tmp_path_factory.mktemp("labels_db") / "synthetic.db"
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
        conn.execute(
            """
            CREATE TABLE markets (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'active',
                resolution TEXT,
                end_date   TEXT
            )
            """
        )

        end_date = (LAST_TS + timedelta(days=30)).isoformat()
        conn.executemany(
            "INSERT INTO markets (id, status, resolution, end_date) "
            "VALUES (?, 'active', NULL, ?)",
            [(mid, end_date) for mid in MARKET_IDS],
        )

        rows = []
        total_hours = int((LAST_TS - FIRST_TS).total_seconds() // 3600)
        for m_idx, mid in enumerate(MARKET_IDS):
            base = 0.20 + 0.10 * m_idx  # distinct price level per market
            for h in range(total_hours + 1):
                ts = FIRST_TS + timedelta(hours=h)
                price = base + 0.001 * h  # steadily rising -> deterministic momentum
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
    """Synthetic (empty) cluster config -- build_training_dataset needs a
    valid path, but no test in this module depends on actual membership;
    that behavior is covered by test_correlation_features.py."""
    path = tmp_path_factory.mktemp("labels_cfg") / "clusters.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def dense_T() -> datetime:
    """A timestamp with dense synthetic coverage: snapshots within ±30 min,
    plus snapshots at T+1h/+6h/+24h (see the ``db`` fixture)."""
    return DENSE_T


# --------------------------------------------------------------------------- #
# generate_outcome_labels
# --------------------------------------------------------------------------- #


def test_normalize_resolution_yes_no_variants():
    for v in ("yes", "YES", "Yes", "y", "Y", "1", "1.0", "true", "TRUE"):
        assert _normalize_resolution(v) == 1, f"{v!r} should be 1"
    for v in ("no", "NO", "No", "n", "N", "0", "0.0", "false", "FALSE"):
        assert _normalize_resolution(v) == 0, f"{v!r} should be 0"


def test_normalize_resolution_unparseable_returns_none():
    for v in (None, "", "  ", "invalid", "0.5", "maybe", "indeterminate"):
        assert _normalize_resolution(v) is None, f"{v!r} should be None"


def test_outcome_labels_empty_when_no_resolved_markets(db):
    # None of the synthetic markets are status='resolved_or_removed' (see
    # the `db` fixture, which inserts them all 'active') -- deterministically
    # exercises the empty-result path, rather than relying on incidental
    # real-DB state "at the time these tests were written".
    df = generate_outcome_labels(list(MARKET_IDS[:3]), db)
    assert list(df.columns) == ["market_id", "outcome"]


def test_outcome_labels_synthetic_db(tmp_path):
    # Build a tiny throwaway DB to exercise the resolution-normalization path.
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE markets (
            id TEXT PRIMARY KEY, status TEXT, resolution TEXT
        )
    """)
    rows = [
        ("a", "resolved_or_removed", "yes"),       # -> 1
        ("b", "resolved_or_removed", "NO"),        # -> 0
        ("c", "resolved_or_removed", "0.5"),       # -> dropped (unparseable)
        ("d", "resolved_or_removed", None),        # -> dropped (null)
        ("e", "active", "yes"),                    # -> dropped (status filter)
        ("f", "resolved_or_removed", ""),          # -> dropped (empty)
        ("g", "resolved_or_removed", "true"),      # -> 1
    ]
    conn.executemany("INSERT INTO markets VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()

    df = generate_outcome_labels(["a", "b", "c", "d", "e", "f", "g"], db)
    assert set(df["market_id"]) == {"a", "b", "g"}
    assert int(df.loc[df.market_id == "a", "outcome"].iloc[0]) == 1
    assert int(df.loc[df.market_id == "b", "outcome"].iloc[0]) == 0
    assert int(df.loc[df.market_id == "g", "outcome"].iloc[0]) == 1


def test_outcome_labels_empty_input():
    df = generate_outcome_labels([], "ignored.db")
    assert df.empty
    assert list(df.columns) == ["market_id", "outcome"]


# --------------------------------------------------------------------------- #
# generate_price_movement_labels
# --------------------------------------------------------------------------- #


def test_price_movement_smoke(db, dense_T):
    df = generate_price_movement_labels([MARKET_IDS[0]], dense_T, db_path=db)
    # Default 3 horizons -> 3 rows.
    assert len(df) == 3
    assert list(df["horizon_hours"]) == [1.0, 6.0, 24.0]
    assert (df["market_id"] == MARKET_IDS[0]).all()

    for _, row in df.iterrows():
        if pd.notna(row["price_change"]):
            # price_change == price_at_t_plus_h - price_at_t (algebraic identity)
            assert row["price_change"] == pytest.approx(
                row["price_at_t_plus_h"] - row["price_at_t"], abs=1e-9
            )
        if pd.notna(row["direction"]):
            assert int(row["direction"]) in (-1, 0, 1)


def test_direction_tolerance_around_threshold(tmp_path):
    """direction=0 when |price_change| < 0.005, ±1 when above."""
    db = tmp_path / "dir.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY, market_id TEXT, timestamp TEXT,
            best_bid REAL, best_ask REAL, bid_depth REAL, ask_depth REAL,
            midpoint REAL, spread REAL, last_price REAL, volume_24h REAL
        )
    """)
    T = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    # Two snapshots: at T (mid=0.500), at T+1h (mid=0.503) => Δ=0.003 -> direction=0
    conn.execute(
        "INSERT INTO snapshots(market_id, timestamp, midpoint) VALUES (?, ?, ?)",
        ("m", T.isoformat(), 0.500),
    )
    conn.execute(
        "INSERT INTO snapshots(market_id, timestamp, midpoint) VALUES (?, ?, ?)",
        ("m", (T.replace(hour=13)).isoformat(), 0.503),
    )
    # At T+6h, mid=0.510 => Δ=+0.010 vs t (NOT vs t+1h) -> direction=+1
    conn.execute(
        "INSERT INTO snapshots(market_id, timestamp, midpoint) VALUES (?, ?, ?)",
        ("m", (T.replace(hour=18)).isoformat(), 0.510),
    )
    # At T+24h, mid=0.490 => Δ=-0.010 -> direction=-1 (clearly past tolerance)
    conn.execute(
        "INSERT INTO snapshots(market_id, timestamp, midpoint) VALUES (?, ?, ?)",
        ("m", (datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)).isoformat(), 0.490),
    )
    conn.commit()
    conn.close()

    df = generate_price_movement_labels(["m"], T, db_path=db)
    by_h = {row["horizon_hours"]: row for _, row in df.iterrows()}

    assert by_h[1.0]["price_change"] == pytest.approx(0.003)
    assert by_h[1.0]["direction"] == 0

    assert by_h[6.0]["price_change"] == pytest.approx(0.010)
    assert by_h[6.0]["direction"] == 1

    # Negative move clearly past the tolerance threshold.
    assert by_h[24.0]["price_change"] == pytest.approx(-0.010)
    assert by_h[24.0]["direction"] == -1


def test_price_movement_outside_window_returns_nulls(db):
    """If T is far outside the snapshot range, all label fields are null."""
    far_future = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    df = generate_price_movement_labels([MARKET_IDS[0]], far_future, db_path=db)
    assert len(df) == 3  # three default horizons
    assert df["price_at_t"].isna().all()
    assert df["price_at_t_plus_h"].isna().all()
    assert df["price_change"].isna().all()
    assert df["direction"].isna().all()


def test_price_movement_empty_inputs(db, dense_T):
    df = generate_price_movement_labels([], dense_T, db_path=db)
    assert df.empty
    df2 = generate_price_movement_labels([MARKET_IDS[0]], dense_T, horizons=[], db_path=db)
    assert df2.empty


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def test_pivot_labels_to_wide_column_naming():
    long = pd.DataFrame([
        {"market_id": "m1", "horizon_hours": 1.0, "price_at_t": 0.5,
         "price_at_t_plus_h": 0.51, "price_change": 0.01, "direction": 1},
        {"market_id": "m1", "horizon_hours": 24.0, "price_at_t": 0.5,
         "price_at_t_plus_h": 0.55, "price_change": 0.05, "direction": 1},
    ])
    wide = _pivot_labels_to_wide(long, [1.0, 24.0])
    assert "price_at_t" in wide.columns
    assert "price_at_t_plus_1h" in wide.columns
    assert "price_at_t_plus_24h" in wide.columns
    assert "price_change_1h" in wide.columns
    assert "direction_24h" in wide.columns
    assert wide.loc[0, "price_change_24h"] == pytest.approx(0.05)


def test_feature_columns_excludes_identifiers_and_labels():
    cols = ["market_id", "timestamp", "price_now", "momentum_1h", "cluster_id",
            "price_at_t", "price_at_t_plus_1h", "price_change_1h", "direction_1h"]
    feats = _feature_columns(cols, [1.0])
    assert "price_now" in feats
    assert "momentum_1h" in feats
    assert "cluster_id" in feats
    for excluded in ("market_id", "timestamp", "price_at_t",
                     "price_at_t_plus_1h", "price_change_1h", "direction_1h"):
        assert excluded not in feats


# --------------------------------------------------------------------------- #
# build_training_dataset
# --------------------------------------------------------------------------- #


def test_build_training_dataset_shape_and_columns(db, cfg, dense_T):
    ids = list(MARKET_IDS)
    ts = [dense_T, SECOND_T]
    df = build_training_dataset(ids, ts, db_path=db, cluster_config_path=cfg)

    # All input rows should pass the >50% null filter for these markets.
    assert len(df) == len(ids) * len(ts)
    # 2 identifiers + (len(MARKET_KEYS) - 1) market features (minus market_id)
    # + (len(CORR_KEYS) - 1) correlation features (minus market_id)
    # + 1 (price_at_t)
    # + 3 horizons × 3 columns each (price_at_t_plus, price_change, direction)
    expected_cols = (
        2  # market_id, timestamp
        + (len(MARKET_KEYS) - 1)
        + (len(CORR_KEYS) - 1)
        + 1
        + 3 * 3
    )
    assert len(df.columns) == expected_cols, f"got {len(df.columns)} cols: {list(df.columns)}"
    assert "market_id" in df.columns
    assert "timestamp" in df.columns
    assert "price_now" in df.columns
    assert "cluster_id" in df.columns
    assert "price_change_1h" in df.columns
    assert "direction_24h" in df.columns


def test_null_fraction_filter_drops_rows(db, cfg, dense_T):
    """Markets that don't exist in the DB at all should produce all-null
    features and be filtered out by the >50% null rule.
    """
    ids = [MARKET_IDS[0], "ghost_market_does_not_exist"]
    df = build_training_dataset(ids, [dense_T], db_path=db, cluster_config_path=cfg)
    # MARKET_IDS[0] keeps its row; the ghost row drops.
    assert MARKET_IDS[0] in set(df["market_id"])
    assert "ghost_market_does_not_exist" not in set(df["market_id"])


def test_build_training_dataset_empty_inputs(db, cfg):
    assert build_training_dataset([], [datetime.now(timezone.utc)], db_path=db, cluster_config_path=cfg).empty
    assert build_training_dataset(["x"], [], db_path=db, cluster_config_path=cfg).empty
