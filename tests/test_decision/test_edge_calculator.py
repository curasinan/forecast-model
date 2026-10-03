"""Tests for ``decision.edge_calculator.compute_edges``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from decision.edge_calculator import (
    EDGE_COLUMNS,
    compute_edges,
)


def _make_db(tmp_path: Path, snaps: list[tuple]) -> Path:
    """Create a throwaway SQLite with a snapshots table and the given rows.

    Each row: (market_id, timestamp, best_bid, best_ask, midpoint, spread)
    """
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE snapshots ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  market_id TEXT, timestamp TEXT,"
        "  best_bid REAL, best_ask REAL, bid_depth REAL, ask_depth REAL,"
        "  midpoint REAL, spread REAL, last_price REAL, volume_24h REAL)"
    )
    for mid, ts, bb, ba, mp, sp in snaps:
        conn.execute(
            "INSERT INTO snapshots(market_id, timestamp, best_bid, best_ask, midpoint, spread) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, ts, bb, ba, mp, sp),
        )
    conn.commit()
    conn.close()
    return db


def test_threshold_filter_keeps_only_above_min_edge(tmp_path):
    """Edge below 0.005 → tradeable=False; above → True."""
    db = _make_db(tmp_path, [
        ("a", "2026-05-26T10:00:00Z", 0.59, 0.61, 0.60, 0.02),  # spread=0.02 → half=0.01
        ("b", "2026-05-26T10:00:00Z", 0.49, 0.51, 0.50, 0.02),
        ("c", "2026-05-26T10:00:00Z", 0.39, 0.41, 0.40, 0.02),
    ])
    preds = pd.DataFrame({
        "market_id": ["a", "b", "c"],
        # Edges after half-spread: 0.010 (clear above), 0.020 (clear above), 0.001 (below).
        # Avoid the exact 0.005 boundary because IEEE754 makes 0.015 - 0.01 ≈ 0.004999...
        "predicted_change": [0.020, 0.030, 0.011],
    })
    result = compute_edges(preds, db_path=db)
    assert list(result.columns) == list(EDGE_COLUMNS)
    by_id = result.set_index("market_id")
    assert bool(by_id.at["a", "tradeable"]) is True   # 0.010 above 0.005
    assert bool(by_id.at["b", "tradeable"]) is True   # 0.020 well above
    assert bool(by_id.at["c", "tradeable"]) is False  # 0.001 below


def test_direction_sign(tmp_path):
    db = _make_db(tmp_path, [
        ("a", "2026-05-26T10:00:00Z", 0.49, 0.51, 0.50, 0.01),
        ("b", "2026-05-26T10:00:00Z", 0.49, 0.51, 0.50, 0.01),
        ("c", "2026-05-26T10:00:00Z", 0.49, 0.51, 0.50, 0.01),
    ])
    preds = pd.DataFrame({
        "market_id": ["a", "b", "c"],
        "predicted_change": [0.05, -0.05, 0.0],
    })
    out = compute_edges(preds, db_path=db).set_index("market_id")
    assert out.at["a", "direction"] == "buy"
    assert out.at["b", "direction"] == "sell"
    # Exact zero → direction is None → not tradeable
    assert out.at["c", "direction"] is None or pd.isna(out.at["c", "direction"])
    assert not out.at["c", "tradeable"]


def test_adjusted_edge_formula(tmp_path):
    """adjusted_edge = abs(predicted_change) - spread/2 — exact."""
    db = _make_db(tmp_path, [
        ("m1", "2026-05-26T10:00:00Z", 0.495, 0.505, 0.50, 0.01),
    ])
    preds = pd.DataFrame({"market_id": ["m1"], "predicted_change": [0.03]})
    out = compute_edges(preds, db_path=db)
    assert out.loc[0, "adjusted_edge"] == pytest.approx(0.03 - 0.005)


def test_missing_snapshot_carries_through_as_not_tradeable(tmp_path):
    """Markets absent from the snapshots table should not crash."""
    db = _make_db(tmp_path, [
        ("a", "2026-05-26T10:00:00Z", 0.49, 0.51, 0.50, 0.02),
    ])
    preds = pd.DataFrame({
        "market_id": ["a", "ghost_does_not_exist"],
        "predicted_change": [0.05, 0.05],
    })
    out = compute_edges(preds, db_path=db).set_index("market_id")
    assert out.at["a", "tradeable"]
    ghost = out.loc["ghost_does_not_exist"]
    assert not ghost["tradeable"]
    # market-data columns NaN for missing snapshot
    assert pd.isna(ghost["best_bid"])
    assert pd.isna(ghost["spread"])


def test_empty_input_returns_empty_df(tmp_path):
    db = _make_db(tmp_path, [])
    preds = pd.DataFrame(columns=["market_id", "predicted_change"])
    out = compute_edges(preds, db_path=db)
    assert out.empty
    assert list(out.columns) == list(EDGE_COLUMNS)


def test_required_columns_check(tmp_path):
    db = _make_db(tmp_path, [])
    bad = pd.DataFrame({"market_id": ["a"]})  # missing predicted_change
    with pytest.raises(RuntimeError, match="predicted_change"):
        compute_edges(bad, db_path=db)
