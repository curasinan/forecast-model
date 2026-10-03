"""Tests for ``decision.position_sizer.size_positions`` and Kelly math."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from decision.position_sizer import (
    _kelly_fraction,
    _load_kelly_inputs,
    size_positions,
)


def _write_metrics(tmp_path: Path, p: float, avg_buy: float, avg_sell: float) -> Path:
    out = tmp_path / "metrics.json"
    out.write_text(json.dumps({
        "profit_simulation": {
            "full": {
                "pct_profitable_overall": p,
                "avg_pnl_per_buy": avg_buy,
                "avg_pnl_per_sell": avg_sell,
            },
        },
    }), encoding="utf-8")
    return out


def _edges_df(rows: list[tuple[str, float, bool]]) -> pd.DataFrame:
    """rows: list of (market_id, adjusted_edge, tradeable)."""
    return pd.DataFrame([
        {
            "market_id": mid,
            "direction": "buy",
            "predicted_change": 0.05,
            "adjusted_edge": edge,
            "current_midpoint": 0.5,
            "best_bid": 0.49,
            "best_ask": 0.51,
            "spread": 0.02,
            "tradeable": trad,
        }
        for mid, edge, trad in rows
    ])


# --- Kelly math ----------------------------------------------------------- #


def test_kelly_fraction_basic():
    # p=0.5, b=5 → f* = 0.5 - 0.5/5 = 0.4
    assert _kelly_fraction(0.5, 5.0) == pytest.approx(0.4)


def test_kelly_fraction_clipped_negative():
    # p=0.3, b=1 → f* = 0.3 - 0.7/1 = -0.4 → clipped to 0
    assert _kelly_fraction(0.3, 1.0) == 0.0


def test_kelly_fraction_zero_b():
    # Defensive: b=0 should not divide by zero
    assert _kelly_fraction(0.6, 0.0) == 0.0


# --- _load_kelly_inputs --------------------------------------------------- #


def test_load_kelly_inputs_happy_path(tmp_path):
    mp = _write_metrics(tmp_path, p=0.5, avg_buy=-0.0046, avg_sell=0.0255)
    ki = _load_kelly_inputs(mp)
    assert ki.p == pytest.approx(0.5)
    assert ki.avg_win == pytest.approx(0.0255)
    assert ki.avg_loss == pytest.approx(0.0046)
    assert ki.b == pytest.approx(0.0255 / 0.0046)


def test_load_kelly_inputs_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="metrics file not found"):
        _load_kelly_inputs(tmp_path / "does_not_exist.json")


def test_load_kelly_inputs_missing_block(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"some_other_block": {}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="profit_simulation.full"):
        _load_kelly_inputs(bad)


def test_load_kelly_inputs_missing_keys(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "profit_simulation": {"full": {"pct_profitable_overall": 0.5}}
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing required Kelly inputs"):
        _load_kelly_inputs(bad)


# --- size_positions ------------------------------------------------------- #


def test_sizing_applies_kelly_and_quarter_multiplier(tmp_path):
    mp = _write_metrics(tmp_path, p=0.5, avg_buy=-0.005, avg_sell=0.020)
    # b = 0.020 / 0.005 = 4 → f* = 0.5 - 0.5/4 = 0.375 → quarter = 0.09375
    # On $1000 bankroll → $93.75 raw, capped at 5% = $50.
    edges = _edges_df([("m1", 0.01, True)])
    sized = size_positions(edges, bankroll=1000.0, metrics_path=mp)
    assert sized.loc[0, "kelly_fraction"] == pytest.approx(0.375)
    # Capped at 5% of bankroll = $50
    assert sized.loc[0, "position_dollars"] == pytest.approx(50.0)
    assert sized.loc[0, "tradeable"]


def test_sizing_respects_5pct_cap(tmp_path):
    mp = _write_metrics(tmp_path, p=0.6, avg_buy=-0.005, avg_sell=0.050)
    # b = 10 → f* = 0.6 - 0.4/10 = 0.56 → quarter = 0.14 → 14% of bankroll
    # Cap is 5%, so position should be exactly 5% regardless.
    edges = _edges_df([("m1", 0.01, True)])
    sized = size_positions(edges, bankroll=1000.0, metrics_path=mp)
    assert sized.loc[0, "position_dollars"] == pytest.approx(50.0)


def test_sizing_below_5_dollar_floor_marks_not_tradeable(tmp_path):
    mp = _write_metrics(tmp_path, p=0.55, avg_buy=-0.005, avg_sell=0.020)
    # On $50 bankroll, even a 5%-of-bankroll cap = $2.50 → below $5 floor.
    edges = _edges_df([("m1", 0.01, True)])
    sized = size_positions(edges, bankroll=50.0, metrics_path=mp)
    assert not sized.loc[0, "tradeable"]
    assert sized.loc[0, "position_dollars"] == 0.0


def test_sizing_negative_kelly_yields_zero(tmp_path):
    # p=0.2, b=2 → f* = 0.2 - 0.8/2 = -0.2 → clipped to 0
    mp = _write_metrics(tmp_path, p=0.2, avg_buy=-0.005, avg_sell=0.010)
    edges = _edges_df([("m1", 0.01, True), ("m2", 0.02, True)])
    sized = size_positions(edges, bankroll=1000.0, metrics_path=mp)
    assert sized["position_dollars"].sum() == 0
    assert sized["tradeable"].sum() == 0


def test_sizing_passes_through_non_tradeable_unchanged(tmp_path):
    mp = _write_metrics(tmp_path, p=0.5, avg_buy=-0.005, avg_sell=0.020)
    edges = _edges_df([("m1", 0.01, True), ("m2", 0.001, False)])
    sized = size_positions(edges, bankroll=1000.0, metrics_path=mp)
    # m1 sized; m2 untouched
    by_id = sized.set_index("market_id")
    assert by_id.at["m1", "tradeable"]
    assert by_id.at["m1", "position_dollars"] > 0
    assert not by_id.at["m2", "tradeable"]
    assert by_id.at["m2", "position_dollars"] == 0


def test_sizing_zero_bankroll_returns_no_positions(tmp_path):
    mp = _write_metrics(tmp_path, p=0.5, avg_buy=-0.005, avg_sell=0.020)
    edges = _edges_df([("m1", 0.01, True)])
    sized = size_positions(edges, bankroll=0.0, metrics_path=mp)
    assert sized["tradeable"].sum() == 0
