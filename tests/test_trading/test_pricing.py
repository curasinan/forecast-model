"""Limit-price rules for BUY orders (src/trading/pricing.py)."""

from __future__ import annotations

import math

import pytest

from src.trading.pricing import buy_limit_price, finite_or_none


class TestAskPolicy:
    def test_zero_ticks_bids_the_ask(self):
        assert buy_limit_price(0.62, 0.65, policy="ask") == 0.65

    def test_ticks_below_ask_steps_down_on_the_grid(self):
        assert buy_limit_price(0.62, 0.65, policy="ask", ticks_below_ask=1) == 0.64
        assert buy_limit_price(0.62, 0.65, policy="ask", ticks_below_ask=2) == 0.63

    def test_one_tick_below_a_float_ask_does_not_lose_a_tick(self):
        # 0.31 - 0.01 is 0.29999999999999993 in floats; the grid must still say 0.30
        assert buy_limit_price(0.28, 0.31, policy="ask", ticks_below_ask=1) == 0.30

    def test_never_bids_below_the_bid(self):
        assert buy_limit_price(0.62, 0.65, policy="ask", ticks_below_ask=10) == 0.62

    def test_no_bid_still_prices_off_the_ask(self):
        assert buy_limit_price(None, 0.31, policy="ask") == 0.31
        assert buy_limit_price(float("nan"), 0.31, policy="ask", ticks_below_ask=1) == 0.30

    def test_coarser_tick_snaps_down(self):
        assert buy_limit_price(0.60, 0.63, policy="ask", tick=0.05) == 0.60


class TestMidPolicy:
    def test_mid_snaps_down_to_the_tick(self):
        assert buy_limit_price(0.62, 0.65, policy="mid") == 0.63

    def test_mid_needs_both_sides(self):
        assert buy_limit_price(None, 0.65, policy="mid") is None


class TestRefusals:
    @pytest.mark.parametrize("bid,ask", [(0.5, None), (0.5, float("nan")), (0.5, -1.0), (None, 0.0)])
    def test_no_usable_ask_is_none(self, bid, ask):
        assert buy_limit_price(bid, ask, policy="ask") is None

    def test_crossed_quote_is_none(self):
        assert buy_limit_price(0.70, 0.65, policy="ask") is None

    def test_below_one_tick_is_none(self):
        assert buy_limit_price(None, 0.005, policy="ask") is None

    def test_unknown_policy_raises(self):
        with pytest.raises(ValueError, match="price policy"):
            buy_limit_price(0.1, 0.2, policy="last")

    def test_negative_ticks_raise(self):
        with pytest.raises(ValueError, match="negative"):
            buy_limit_price(0.1, 0.2, policy="ask", ticks_below_ask=-1)

    def test_non_positive_tick_raises(self):
        with pytest.raises(ValueError, match="tick"):
            buy_limit_price(0.1, 0.2, policy="ask", tick=0.0)


def test_finite_or_none():
    assert finite_or_none("0.5") == 0.5
    assert finite_or_none(None) is None
    assert finite_or_none("x") is None
    assert finite_or_none(math.inf) is None
    assert finite_or_none(float("nan")) is None
