"""Tests for ``decision.risk_manager.apply_risk_limits``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from decision.risk_manager import (
    DEFAULT_RISK_CONFIG,
    apply_risk_limits,
    init_db,
    load_risk_config,
)


def _make_db(
    tmp_path: Path,
    open_positions: list[dict] | None = None,
    closed_positions: list[dict] | None = None,
) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    init_db(conn)
    for pos in (open_positions or []):
        conn.execute(
            "INSERT INTO paper_positions(market_id, direction, position_dollars, "
            "cluster, opened_at, status) VALUES (?, ?, ?, ?, ?, 'open')",
            (pos["market_id"], pos["direction"], pos["position_dollars"],
             pos.get("cluster"), pos.get("opened_at", "2026-05-01T00:00:00Z")),
        )
    for pos in (closed_positions or []):
        conn.execute(
            "INSERT INTO paper_positions(market_id, direction, position_dollars, "
            "cluster, opened_at, closed_at, status, realized_pnl) VALUES "
            "(?, ?, ?, ?, ?, ?, 'closed', ?)",
            (pos["market_id"], pos["direction"], pos["position_dollars"],
             pos.get("cluster"), pos.get("opened_at", "2026-05-01T00:00:00Z"),
             pos.get("closed_at", "2026-05-15T00:00:00Z"), pos["realized_pnl"]),
        )
    conn.commit()
    conn.close()
    return db


def _make_cluster_config(tmp_path: Path, clusters: dict[str, list[str]]) -> Path:
    p = tmp_path / "clusters.json"
    p.write_text(json.dumps(clusters), encoding="utf-8")
    return p


def _make_risk_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    p = tmp_path / "risk_limits.json"
    cfg = {**DEFAULT_RISK_CONFIG, **(overrides or {})}
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _sized_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "market_id": "",
        "direction": "buy",
        "predicted_change": 0.03,
        "adjusted_edge": 0.01,
        "current_midpoint": 0.5,
        "best_bid": 0.49, "best_ask": 0.51, "spread": 0.02,
        "tradeable": True,
        "kelly_fraction": 0.3,
        "position_dollars": 40.0,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def test_rule1_drawdown_trips_at_20pct(tmp_path):
    db = _make_db(tmp_path, closed_positions=[
        {"market_id": "x", "direction": "buy", "position_dollars": 100,
         "realized_pnl": -250.0},  # 25% drawdown on $1000
    ])
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([{"market_id": "a", "position_dollars": 40.0}])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert not out["tradeable"].any()
    assert (out["rejected_reason"] == "drawdown").any()


def test_rule1_no_drawdown_when_realized_zero(tmp_path):
    db = _make_db(tmp_path)
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([{"market_id": "a", "position_dollars": 40.0}])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert out.loc[0, "tradeable"]


def test_rule2_cluster_cap_scales_within_cluster(tmp_path):
    """Two $60 signals in same 'iran' cluster → scale to total = $100 (10% cap)."""
    db = _make_db(tmp_path)
    cc = _make_cluster_config(tmp_path, {"iran": ["a", "b"]})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([
        {"market_id": "a", "position_dollars": 60.0, "adjusted_edge": 0.020},
        {"market_id": "b", "position_dollars": 60.0, "adjusted_edge": 0.010},
    ])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    total_in_cluster = out.loc[out["tradeable"], "position_dollars"].sum()
    # 100 / 120 = 0.8333 scale → each goes to $50 → total = $100 cap
    assert total_in_cluster == pytest.approx(100.0)


def test_rule2_existing_cluster_exposure_reduces_headroom(tmp_path):
    """$60 already open in iran cluster → new $60 scaled to $40 ($100 cap)."""
    db = _make_db(tmp_path, open_positions=[
        {"market_id": "a", "direction": "buy", "position_dollars": 60.0, "cluster": "iran"},
    ])
    cc = _make_cluster_config(tmp_path, {"iran": ["a", "b"]})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([{"market_id": "b", "position_dollars": 60.0}])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert out.loc[0, "position_dollars"] == pytest.approx(40.0)


def test_rule2_singleton_clusters_for_unclustered_markets(tmp_path):
    """Markets not in any cluster get their own bucket → independent caps."""
    db = _make_db(tmp_path)
    cc = _make_cluster_config(tmp_path, {})  # empty cluster map
    rc = _make_risk_config(tmp_path)

    # Two $60 signals on $1000 → cap $100 each → both pass individually
    sized = _sized_df([
        {"market_id": "a", "position_dollars": 60.0},
        {"market_id": "b", "position_dollars": 60.0},
    ])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert (out["position_dollars"] == 60.0).all()


def test_rule3_total_cap_scales_everything(tmp_path):
    """10 × $40 new signals + $40 existing = $440 > $350 cap → scale."""
    db = _make_db(tmp_path, open_positions=[
        {"market_id": "x", "direction": "buy", "position_dollars": 40.0,
         "cluster": "_singleton_x"},
    ])
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([
        {"market_id": f"m{i}", "position_dollars": 40.0, "adjusted_edge": 0.01 + 0.001 * i}
        for i in range(10)
    ])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert out.loc[out["tradeable"], "position_dollars"].sum() == pytest.approx(310.0)


def test_rule4_max_positions_keeps_highest_edge(tmp_path):
    """11 open positions + 3 new → 1 slot → keep highest-edge new signal."""
    db = _make_db(tmp_path, open_positions=[
        {"market_id": f"open{i}", "direction": "buy", "position_dollars": 10.0,
         "cluster": f"_singleton_open{i}"}
        for i in range(11)
    ])
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([
        {"market_id": "low",  "position_dollars": 20.0, "adjusted_edge": 0.005},
        {"market_id": "high", "position_dollars": 20.0, "adjusted_edge": 0.050},
        {"market_id": "mid",  "position_dollars": 20.0, "adjusted_edge": 0.020},
    ])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc).set_index("market_id")
    assert out.at["high", "tradeable"]
    assert not out.at["mid", "tradeable"]
    assert not out.at["low", "tradeable"]
    assert out.at["mid", "rejected_reason"] == "max_concurrent"


def test_rule4_all_slots_full_rejects_all(tmp_path):
    db = _make_db(tmp_path, open_positions=[
        {"market_id": f"open{i}", "direction": "buy", "position_dollars": 10.0,
         "cluster": f"_singleton_open{i}"}
        for i in range(12)
    ])
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)
    sized = _sized_df([{"market_id": "n1", "position_dollars": 20.0}])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert not out.loc[0, "tradeable"]
    assert out.loc[0, "rejected_reason"] == "max_concurrent"


def test_drawdown_short_circuits_other_rules(tmp_path):
    """Rule 1 firing means rules 2/3/4 don't matter — everything rejected."""
    db = _make_db(tmp_path, closed_positions=[
        {"market_id": "x", "direction": "buy", "position_dollars": 100.0,
         "realized_pnl": -300.0},  # 30% drawdown
    ])
    cc = _make_cluster_config(tmp_path, {"iran": ["a", "b"]})
    rc = _make_risk_config(tmp_path)

    sized = _sized_df([
        {"market_id": "a", "position_dollars": 40.0},
        {"market_id": "b", "position_dollars": 40.0},
    ])
    out = apply_risk_limits(sized, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert not out["tradeable"].any()
    assert (out["rejected_reason"] == "drawdown").all()


def test_empty_input_returns_empty(tmp_path):
    db = _make_db(tmp_path)
    cc = _make_cluster_config(tmp_path, {})
    rc = _make_risk_config(tmp_path)
    empty = pd.DataFrame(columns=[
        "market_id", "direction", "predicted_change", "adjusted_edge",
        "current_midpoint", "best_bid", "best_ask", "spread",
        "tradeable", "kelly_fraction", "position_dollars",
    ])
    out = apply_risk_limits(empty, bankroll=1000.0, db_path=db,
                            cluster_config_path=cc, risk_config_path=rc)
    assert out.empty


def test_load_risk_config_creates_default_when_missing(tmp_path):
    cfg_path = tmp_path / "subdir" / "risk_limits.json"
    cfg = load_risk_config(cfg_path)
    assert cfg_path.exists()
    assert cfg["starting_bankroll"] == DEFAULT_RISK_CONFIG["starting_bankroll"]
