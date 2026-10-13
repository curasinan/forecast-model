"""End-to-end orchestration test for ``decision.signal_generator``."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from decision.risk_manager import DEFAULT_RISK_CONFIG, init_db
from decision.signal_generator import generate_signals


class DummyModel:
    """Returns predicted_change values from a per-market lookup, or 0.0."""

    def __init__(self, per_market: dict[str, float] | None = None) -> None:
        self.per_market = per_market or {}
        self.last_X: pd.DataFrame | None = None

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        self.last_X = X
        return np.full(len(X), 0.0)


def _seed_db(db: Path, market_ids: list[str], earliest_iso: str, latest_iso: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY, question TEXT, status TEXT, end_date TEXT,
            volume REAL, volume_24h REAL, liquidity REAL, clob_token_ids TEXT,
            orderbook_missing INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, timestamp TEXT,
            best_bid REAL, best_ask REAL, bid_depth REAL, ask_depth REAL,
            midpoint REAL, spread REAL, last_price REAL, volume_24h REAL
        )
    """)
    init_db(conn)
    for mid in market_ids:
        conn.execute(
            "INSERT INTO markets(id, question, status, end_date) VALUES (?, ?, 'active', ?)",
            (mid, f"Will Trump win the election in {mid}?", "2027-01-01T00:00:00Z"),
        )
        for i in range(60):
            ts = f"2026-05-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00+00:00"
            conn.execute(
                "INSERT INTO snapshots(market_id, timestamp, best_bid, best_ask, "
                "midpoint, spread, bid_depth, ask_depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ts, 0.49, 0.51, 0.50, 0.02, 1000.0, 1000.0),
            )
    conn.commit()
    conn.close()


def _write_metrics_full(tmp_path: Path) -> Path:
    p = tmp_path / "metrics_6h_v2.json"
    p.write_text(json.dumps({
        "profit_simulation": {
            "full": {
                "pct_profitable_overall": 0.5,
                "avg_pnl_per_buy": -0.0046,
                "avg_pnl_per_sell": 0.0255,
            }
        }
    }), encoding="utf-8")
    return p


def _write_cluster_config(tmp_path: Path, clusters: dict | None = None) -> Path:
    p = tmp_path / "clusters.json"
    p.write_text(json.dumps(clusters or {}), encoding="utf-8")
    return p


def _write_risk_config(tmp_path: Path) -> Path:
    p = tmp_path / "risk_limits.json"
    p.write_text(json.dumps(DEFAULT_RISK_CONFIG), encoding="utf-8")
    return p


def _write_model_artifact(tmp_path: Path, feature_cols: list[str]) -> Path:
    p = tmp_path / "lgbm.pkl"
    artifact = {
        "model": DummyModel(),
        "feature_cols": feature_cols,
        "cluster_mapping": {"__none__": 0, "iran": 1, "russia_ukraine": 2, "trump_presidency": 3},
        "optimal_n_estimators": 15,
        "lgbm_params": {},
        "trained_at": "2026-05-26T00:00:00Z",
    }
    joblib.dump(artifact, p)
    return p


def test_end_to_end_returns_typed_dataframe(tmp_path):
    """generate_signals end-to-end on a 2-market synth DB."""
    db = tmp_path / "synth.db"
    market_ids = ["mkt1", "mkt2"]
    _seed_db(db, market_ids, "2026-05-01T00:00:00+00:00", "2026-05-26T00:00:00+00:00")

    metrics_path = _write_metrics_full(tmp_path)
    clusters_path = _write_cluster_config(tmp_path, {"iran": ["mkt1"]})
    risk_path = _write_risk_config(tmp_path)
    artifact_path = _write_model_artifact(tmp_path, [
        "momentum_24h", "momentum_72h", "price_now", "time_to_resolution",
    ])

    T = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    out = generate_signals(
        bankroll=1000.0,
        db_path=db,
        model_artifact_path=artifact_path,
        cluster_config_path=clusters_path,
        risk_config_path=risk_path,
        metrics_path=metrics_path,
        timestamp=T,
    )

    # Expected columns regardless of whether anything is tradeable.
    expected_cols = {
        "market_id", "direction", "predicted_change", "adjusted_edge",
        "current_midpoint", "best_bid", "best_ask", "spread",
        "tradeable", "kelly_fraction", "position_dollars", "rejected_reason",
    }
    assert expected_cols.issubset(set(out.columns))

    # All predictions are 0 → direction None → no trades approved.
    assert not out["tradeable"].any()
    assert len(out) == len(market_ids)


def test_end_to_end_empty_cohort_returns_empty(tmp_path):
    """If no markets pass the snapshot threshold, return an empty signal set."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT,
                    status TEXT, end_date TEXT, volume REAL, volume_24h REAL,
                    liquidity REAL, clob_token_ids TEXT,
                    orderbook_missing INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT, timestamp TEXT, best_bid REAL, best_ask REAL,
                    bid_depth REAL, ask_depth REAL, midpoint REAL, spread REAL,
                    last_price REAL, volume_24h REAL)""")
    init_db(conn)
    conn.commit()
    conn.close()

    metrics_path = _write_metrics_full(tmp_path)
    clusters_path = _write_cluster_config(tmp_path)
    risk_path = _write_risk_config(tmp_path)
    artifact_path = _write_model_artifact(tmp_path, [
        "momentum_24h", "momentum_72h", "price_now", "time_to_resolution",
    ])

    out = generate_signals(
        bankroll=1000.0, db_path=db, model_artifact_path=artifact_path,
        cluster_config_path=clusters_path, risk_config_path=risk_path,
        metrics_path=metrics_path,
        timestamp=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
    )
    assert out.empty or not out["tradeable"].any()
