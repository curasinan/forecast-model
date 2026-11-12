"""Exact settlement distributions of a GTM book (climate/sim/book.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from climate.sim.book import (
    Position,
    Scenario,
    book_cost,
    breakeven_mu,
    build_scenarios,
    mark_to_market,
    outcome_table,
    settlement_value,
    summarize,
)
from climate.sim.implied import ImpliedFit

BOOK = [Position(1.3, "YES", 22, 0.44), Position(1.2, "YES", 8, 0.65)]


def test_a_print_on_the_strike_pays_no_not_yes():
    assert settlement_value([Position(1.3, "YES", 10, 0.4)], 1.30) == 0.0
    assert settlement_value([Position(1.3, "YES", 10, 0.4)], 1.31) == 10.0
    assert settlement_value([Position(1.3, "NO", 10, 0.6)], 1.30) == 10.0


def test_cost_includes_the_commission():
    assert book_cost(BOOK, 0.01) == pytest.approx(22 * 0.45 + 8 * 0.66)


def test_outcome_table_is_exact_and_sums_to_one():
    mu, sd = 1.4251, 0.1149
    table = outcome_table(BOOK, mu, sd)
    assert table["prob"].sum() == pytest.approx(1.0)
    cost = book_cost(BOOK, 0.01)
    assert table["pnl"].tolist() == pytest.approx([-cost, 8 - cost, 30 - cost])
    p13 = norm.sf((1.305 - mu) / sd)
    p12 = norm.sf((1.205 - mu) / sd)
    assert table["prob"].iloc[2] == pytest.approx(p13)
    assert table["prob"].iloc[1] == pytest.approx(p12 - p13)


def test_summary_matches_the_hand_calculation():
    mu, sd = 1.4251, 0.1149
    cost = book_cost(BOOK, 0.01)
    s = summarize(outcome_table(BOOK, mu, sd), cost)
    p13, p12 = norm.sf((1.305 - mu) / sd), norm.sf((1.205 - mu) / sd)
    assert s["expected_pnl"] == pytest.approx(22 * p13 + 8 * p12 - cost)
    assert s["p_total_loss"] == pytest.approx(1 - p12)
    assert s["p_loss"] == pytest.approx(1 - p13)
    assert s["worst"] == pytest.approx(-cost) and s["best"] == pytest.approx(30 - cost)


def test_breakeven_mu_zeroes_the_expected_pnl():
    mu = breakeven_mu(BOOK, 0.12)
    s = summarize(outcome_table(BOOK, mu, 0.12), book_cost(BOOK, 0.01))
    assert s["expected_pnl"] == pytest.approx(0.0, abs=1e-4)


def test_scenarios_blend_the_base_model_with_an_identified_market():
    model = Scenario("model", 1.42, 0.12, "test")
    market = ImpliedFit(1.30, 0.18, 0.93, True, 17, 3, 0.01, True)
    out = {s.name: s for s in build_scenarios([model], market, blends=(0.5,),
                                              mu_shifts=(-0.1,), sigma_scales=(1.5,))}
    assert out["blend_w0.5"].mu == pytest.approx(1.36)
    assert out["blend_w0.5"].sigma == pytest.approx(0.15)
    assert out["model_mu-0.1"].mu == pytest.approx(1.32)
    assert out["model_sigma_x1.5"].sigma == pytest.approx(0.18)
    assert "market" in out


def test_an_unidentified_market_gives_no_market_scenario():
    model = Scenario("model", 1.42, 0.12, "test")
    market = ImpliedFit(1.30, 0.18, 0.93, True, 11, 1, 0.01, False)
    names = [s.name for s in build_scenarios([model], market)]
    assert "market" not in names and not any(n.startswith("blend") for n in names)


def test_mark_to_market_at_mid_and_bid():
    quotes = pd.DataFrame({"strike": [1.2, 1.3], "bid": [0.62, 0.41], "ask": [0.65, 0.44]})
    m = mark_to_market(BOOK, quotes)
    assert m["value_at_mid"] == pytest.approx(22 * 0.425 + 8 * 0.635)
    assert m["value_at_bid"] == pytest.approx(22 * 0.41 + 8 * 0.62)
    assert m["unpriced_strikes"] == []


@pytest.mark.parametrize("text", ["1.3:YES:22", "1.3:MAYBE:22:0.4", "1.3:YES:0:0.4", "1.3:YES:5:1.2"])
def test_bad_positions_are_refused(text):
    with pytest.raises(ValueError):
        Position.parse(text)


def test_bad_distribution_is_refused():
    with pytest.raises(ValueError):
        outcome_table(BOOK, 1.4, 0.0)
    with pytest.raises(ValueError):
        outcome_table(BOOK, np.nan, 0.1)
