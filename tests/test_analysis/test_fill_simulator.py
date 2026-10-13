"""Tests for the maker-fill simulator."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import analysis.fill_simulator as fs

BASE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def build(rows: list[tuple]) -> list[dict]:
    """rows = [(minute, best_bid, best_ask, midpoint), ...] -> snapshot dicts."""
    out: list[dict] = []
    for minute, bid, ask, mid in rows:
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        out.append({
            "ts": BASE + timedelta(minutes=minute),
            "best_bid": bid, "best_ask": ask, "midpoint": mid, "spread": spread,
        })
    return out


# Fill detection + direction


def test_fill_detection_and_direction():
    snaps = build([
        (0,   0.49, 0.51,  0.50),
        (60,  0.48, 0.485, 0.4825),   # ask trades through a 0.49 buy limit
        (360, 0.49, 0.51,  0.50),
    ])
    buy1 = fs.simulate_order(snaps, 0, +1, 0.01)   # P = 0.49
    assert buy1["filled"] is True
    assert buy1["time_to_fill_min"] == pytest.approx(60.0)
    # Booked at the LIMIT (0.49), not the better 0.485 touch.
    assert buy1["fill_price"] == pytest.approx(0.49)

    assert fs.simulate_order(snaps, 0, +1, 0.02)["filled"] is False   # P=0.48, ask never <=
    assert fs.simulate_order(snaps, 0, -1, 0.01)["filled"] is False   # P=0.51, bid never >=
    assert fs.simulate_order(snaps, 0, -1, 0.02)["filled"] is False   # P=0.52


# Adverse-selection sign math


def test_adverse_buy_sign():
    # Buy fills at 0.49; price keeps falling to a 0.45 mark-out -> adverse.
    snaps = build([
        (0,   0.49, 0.51, 0.50),
        (60,  0.48, 0.49, 0.485),
        (360, 0.44, 0.46, 0.45),
    ])
    o = fs.simulate_order(snaps, 0, +1, 0.01)
    assert o["filled"] and o["mid_end"] == pytest.approx(0.45)
    assert o["drift"] == pytest.approx(-0.05)         # +1*(0.45-0.50)
    assert o["adverse_cost"] == pytest.approx(0.05)
    assert o["capture"] == pytest.approx(0.01)
    assert o["net_oneleg"] == pytest.approx(-0.04)    # 0.45 - 0.49


def test_adverse_sell_sign():
    snaps = build([
        (0,   0.49, 0.51, 0.50),
        (60,  0.51, 0.53, 0.52),
        (360, 0.59, 0.61, 0.60),
    ])
    o = fs.simulate_order(snaps, 0, -1, 0.01)         # P = 0.51
    assert o["filled"] and o["time_to_fill_min"] == pytest.approx(60.0)
    assert o["drift"] == pytest.approx(-0.10)         # -1*(0.60-0.50)
    assert o["adverse_cost"] == pytest.approx(0.10)
    assert o["net_oneleg"] == pytest.approx(-0.09)    # 0.51 - 0.60


def test_net_favorable_buy():
    snaps = build([
        (0,   0.49, 0.51, 0.50),
        (60,  0.48, 0.49, 0.485),
        (360, 0.54, 0.56, 0.55),
    ])
    o = fs.simulate_order(snaps, 0, +1, 0.01)
    assert o["adverse_cost"] == pytest.approx(-0.05)
    assert o["net_oneleg"] == pytest.approx(0.06)     # 0.55 - 0.49
    assert o["net_oneleg"] > 0


# Mark-out gating + density


def test_no_markout_excludes_from_cost_but_not_fill():
    snaps = build([
        (0,   0.49, 0.51, 0.50),
        (60,  0.48, 0.49, 0.485),
        (300, 0.50, 0.52, 0.51),
    ])
    o = fs.simulate_order(snaps, 0, +1, 0.01)
    assert o["filled"] is True                 # fill still detected
    assert o["has_markout"] is False
    assert o["mid_end"] is None
    assert o["drift"] is None and o["adverse_cost"] is None and o["net_oneleg"] is None

    agg = fs.aggregate_offset([o])
    assert agg["n_filled"] == 1
    assert agg["fill_rate"] == pytest.approx(1.0)
    assert agg["avg_adverse_cost"] is None     # excluded from cost stats
    assert agg["n_filled_markout"] == 0


def test_density_flag():
    dense = build([(m, 0.49, 0.51, 0.50) for m in range(0, 361, 30)])   # 30-min cadence
    assert fs.simulate_order(dense, 0, +1, 0.02)["dense"] is True

    sparse = build([(m, 0.49, 0.51, 0.50)
                    for m in (0, 30, 60, 180, 210, 240, 270, 300, 330, 360)])  # 120-min gap
    assert fs.simulate_order(sparse, 0, +1, 0.02)["dense"] is False


# Order generation: pin exclusion + 12h spacing


def test_pinned_market_generates_no_orders():
    snaps = build([(m, 0.97, 0.99, 0.98) for m in range(0, 361, 15)])  # mid pinned at 0.98
    orders, stats = fs.generate_orders_for_market(snaps)
    assert orders == []
    assert stats["posttimes"] == 0
    assert stats["dropped_pin"] > 0


def test_post_spacing_is_12h():
    snaps = build([(m, 0.49, 0.51, 0.50) for m in range(0, 48 * 60 + 1, 15)])
    orders, stats = fs.generate_orders_for_market(snaps)
    assert stats["posttimes"] == 4
    assert len(orders) == 16
    assert all(o["delta"] in fs.OFFSETS for o in orders)
    assert {o["side_sign"] for o in orders} == {+1, -1}


# Political cohort selection (tmp_path DB)


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT)")
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, best_bid REAL, best_ask REAL, "
        "midpoint REAL, spread REAL)"
    )

    def add(mid, question, n):
        conn.execute("INSERT INTO markets VALUES (?, ?)", (mid, question))
        for h in range(n):
            conn.execute(
                "INSERT INTO snapshots (market_id, timestamp, best_bid, best_ask, "
                "midpoint, spread) VALUES (?, ?, 0.49, 0.51, 0.50, 0.02)",
                (mid, (BASE + timedelta(hours=h)).isoformat()),
            )

    add("m_pol", "Will Trump win the 2024 election?", 6)      # political, enough snaps
    add("m_non", "Will it rain in Seattle tomorrow?", 6)      # not political
    add("m_small", "Senate impeachment vote outcome?", 3)     # political but too few
    conn.commit()
    conn.close()
    return db


def test_political_cohort_selection(tmp_path):
    db = _make_db(tmp_path)
    ids = fs.load_political_market_ids(db, min_snapshots=5)
    assert set(ids) == {"m_pol"}


# Aggregation arithmetic + buffer verdict


def _order(filled, dense, has_markout, drift, ttf, net_oneleg):
    return {
        "side_sign": +1, "delta": 0.01, "mid_0": 0.50, "limit_price": 0.49,
        "filled": filled, "time_to_fill_min": ttf, "fill_price": 0.49 if filled else None,
        "has_forward": True, "dense": dense,
        "mid_end": 0.50 if has_markout else None, "has_markout": has_markout,
        "drift": drift, "adverse_cost": (-drift) if drift is not None else None,
        "capture": 0.01, "net_oneleg": net_oneleg, "net_roundtrip": None,
    }


def test_aggregation_arithmetic():
    orders = [
        _order(True,  True,  True,  -0.006, 30, +0.004),
        _order(True,  True,  True,  -0.010, 90, +0.000),
        _order(False, True,  True,  +0.002, None, None),   # unfilled but marks out
        _order(True,  False, False, None,   60, None),     # filled, no mark-out
    ]
    agg = fs.aggregate_offset(orders)

    assert agg["n_orders"] == 4
    assert agg["n_filled"] == 3
    assert agg["fill_rate"] == pytest.approx(0.75)
    assert agg["n_dense"] == 3
    assert agg["dense_fill_rate"] == pytest.approx(2 / 3)   # o1,o2 filled of 3 dense
    assert agg["avg_time_to_fill_min"] == pytest.approx(60.0)   # (30+90+60)/3
    assert agg["n_filled_markout"] == 2
    assert agg["avg_adverse_cost"] == pytest.approx(0.008)      # mean(0.006, 0.010)
    assert agg["avg_net_oneleg"] == pytest.approx(0.002)        # mean(0.004, 0.000)
    assert agg["avg_net_roundtrip"] is None
    assert agg["unconditional_drift"] == pytest.approx(-0.014 / 3)   # over 3 mark-outs
    assert agg["filled_drift"] == pytest.approx(-0.008)
    assert agg["adverse_selection"] == pytest.approx(-0.008 - (-0.014 / 3))
    assert agg["buffer_verdict"] == "optimistic"               # 0.008 > 0.005


def test_buffer_verdict_thresholds():
    assert fs.buffer_verdict(0.008) == "optimistic"
    assert fs.buffer_verdict(0.005) == "realistic"
    assert fs.buffer_verdict(0.001) == "conservative"
    assert fs.buffer_verdict(-0.02) == "conservative"
    assert fs.buffer_verdict(None) == "no_data"
