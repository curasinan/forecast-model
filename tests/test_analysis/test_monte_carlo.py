"""Tests for the Monte Carlo bankroll simulator."""

from __future__ import annotations

import numpy as np
import pytest

import analysis.monte_carlo as mc


# Kelly sizing under caps


def test_full_kelly_fraction():
    assert mc.full_kelly_fraction(0.02) == pytest.approx(0.04)
    assert mc.full_kelly_fraction(0.0) == pytest.approx(0.0)


def test_position_size_floor_branches():
    kw = dict(believed_edge=0.02, kelly_multiplier=0.25, min_position=5.0, max_pct=0.05)
    # quarter-Kelly fraction = 0.25 * 2 * 0.02 = 0.01 of bankroll
    assert mc.position_size(500, **kw) == pytest.approx(5.0)    # desired 5 == floor
    assert mc.position_size(2000, **kw) == pytest.approx(20.0)  # desired 20, uncapped
    assert mc.position_size(300, **kw) == pytest.approx(5.0)
    assert mc.position_size(50, **kw) == pytest.approx(2.5)     # 5% of 50
    # high believed edge -> 5% max cap binds
    assert mc.position_size(500, believed_edge=0.20, kelly_multiplier=0.25,
                            min_position=5.0, max_pct=0.05) == pytest.approx(25.0)
    assert mc.position_size(0.0, **kw) == 0.0


def test_position_size_gate_mode():
    kw = dict(believed_edge=0.02, kelly_multiplier=0.25, min_position=5.0,
              max_pct=0.05, mode="gate")
    assert mc.position_size(300, **kw) == 0.0        # desired 3 < 5 -> skipped
    assert mc.position_size(500, **kw) == pytest.approx(5.0)   # desired 5 -> placed
    assert mc.position_size(2000, **kw) == pytest.approx(20.0)


def test_drag_fraction():
    assert mc.drag_fraction(0.03125) == pytest.approx(0.0625)
    assert mc.drag_fraction(0.05, entry_price=0.5) == pytest.approx(0.10)


# Drawdown + ruin on deterministic paths


def test_max_drawdown_known_path():
    path = np.array([500, 550, 400, 450, 300], dtype=float)
    # peak 550; deepest trough 300 -> (550-300)/550
    assert mc.max_drawdown(path) == pytest.approx(250 / 550)


def test_max_drawdown_monotone_increasing_is_zero():
    assert mc.max_drawdown(np.array([100, 200, 300], dtype=float)) == pytest.approx(0.0)


# Deterministic simulator behavior (fill_rate=1, win_prob pinned)


def _sim(true_edge, n_bets, adverse_cost=0.0, n_runs=1, **kw):
    rng = np.random.default_rng(0)
    return mc.simulate_scenario(
        rng, true_edge, believed_edge=0.02, starting=500.0, n_bets=n_bets,
        n_runs=n_runs, fill_rate=1.0, adverse_cost=adverse_cost, **kw,
    )


def test_always_win_grows_no_drag():
    # true_edge=+0.5 -> win_prob=1.0 (always win); fill_rate=1; no drag.
    out = _sim(0.5, n_bets=2)
    # 500 -> +5 -> 505 -> +5.05 -> 510.05
    assert out["finals"][0] == pytest.approx(510.05)
    assert out["max_dds"][0] == pytest.approx(0.0)
    assert out["fills"][0] == 2
    assert bool(out["ruined"][0]) is False


def test_always_lose_drawdown_no_drag():
    out = _sim(-0.5, n_bets=2)  # win_prob=0 -> always lose
    # 500 -> -5 -> 495 -> -5 -> 490
    assert out["finals"][0] == pytest.approx(490.0)
    assert out["max_dds"][0] == pytest.approx(10 / 500)   # 0.02
    assert out["fills"][0] == 2


def test_drag_applied_to_payout():
    win = _sim(0.5, n_bets=1, adverse_cost=0.03125)   # +5*(1-0.0625)
    assert win["finals"][0] == pytest.approx(504.6875)
    lose = _sim(-0.5, n_bets=1, adverse_cost=0.03125)  # -5*(1+0.0625)
    assert lose["finals"][0] == pytest.approx(494.6875)


def test_sustained_losses_trigger_ruin():
    out = _sim(-0.5, n_bets=200, adverse_cost=0.03125, n_runs=3)
    assert out["ruined"].all()
    assert out["max_dds"].min() > 0.8   # dropped >80% from the $500 peak


# Summarize + RNG reproducibility + fair-game sanity


def test_summarize_metrics():
    finals = np.array([100, 200, 300, 400, 500], dtype=float)
    max_dds = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    ruined = np.array([True, False, False, False, False])
    fills = np.array([10, 10, 10, 10, 10])
    s = mc.summarize(finals, max_dds, ruined, fills, starting=300.0)
    assert s["median_final"] == pytest.approx(300.0)
    assert s["prob_below_start"] == pytest.approx(0.4)   # 100,200 < 300
    assert s["prob_profit"] == pytest.approx(0.4)        # 400,500 > 300
    assert s["prob_ruin"] == pytest.approx(0.2)
    assert s["median_max_drawdown"] == pytest.approx(0.3)
    assert s["mean_fills"] == pytest.approx(10.0)


def test_seed_reproducibility():
    a = mc.simulate_scenario(np.random.default_rng(42), 0.02, n_runs=500, n_bets=50)
    b = mc.simulate_scenario(np.random.default_rng(42), 0.02, n_runs=500, n_bets=50)
    assert np.array_equal(a["finals"], b["finals"])
    # a different seed diverges
    c = mc.simulate_scenario(np.random.default_rng(7), 0.02, n_runs=500, n_bets=50)
    assert not np.array_equal(a["finals"], c["finals"])


def test_fair_game_median_near_start():
    rng = np.random.default_rng(123)
    out = mc.simulate_scenario(
        rng, 0.0, believed_edge=0.02, starting=500.0, n_bets=200, n_runs=20_000,
        fill_rate=1.0, adverse_cost=0.0,
    )
    median = float(np.median(out["finals"]))
    assert median == pytest.approx(500.0, abs=15.0)


def test_load_risk_config_fallback(tmp_path):
    missing = tmp_path / "nope.json"
    cfg = mc.load_risk_config(missing)
    assert cfg["kelly_multiplier"] == pytest.approx(0.25)
    assert cfg["min_position_dollars"] == pytest.approx(5.0)
    assert cfg["max_single_position_pct"] == pytest.approx(0.05)
