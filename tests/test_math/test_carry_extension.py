"""Fee/carry/holding-period extensions to EV and Kelly (migration spec Stage 4.3)."""

from datetime import date

import pytest

from src.domain.carry import CarryModel, ForecastExCouponCarry
from src.domain.fees import FlatCommissionFeeModel
from src.math.expected_value import best_trade, expected_value, expected_value_no
from src.math.kelly import (
    empirical_kelly,
    half_kelly,
    kelly_for_no,
    kelly_fraction,
    position_size,
)

P_GRID = [0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98]
PRICE_GRID = [0.005, 0.02, 0.05, 0.1, 0.25, 0.35, 0.5, 0.65, 0.75, 0.9, 0.95, 0.98, 0.995]


class FixedDailyCarry(CarryModel):
    """Deterministic test double: fixed fraction of position value per day."""

    def __init__(self, rate: float):
        self._rate = rate

    def daily_accrual_rate(self) -> float:
        return self._rate


def _legacy_ev(p: float, price: float) -> float:
    profit_if_yes = 1.0 - price
    loss_if_no = price
    return (p * profit_if_yes) - ((1 - p) * loss_if_no)


def _legacy_ev_no(p: float, price_yes: float) -> float:
    no_price = 1.0 - price_yes
    prob_no = 1.0 - p
    profit_if_no = 1.0 - no_price
    loss_if_yes = no_price
    return (prob_no * profit_if_no) - (p * loss_if_yes)


def _legacy_best_trade(p: float, price_yes: float) -> tuple[str, float]:
    ev_yes = _legacy_ev(p, price_yes)
    ev_no = _legacy_ev_no(p, price_yes)
    if ev_yes > ev_no and ev_yes > 0:
        return "YES", ev_yes
    elif ev_no > ev_yes and ev_no > 0:
        return "NO", ev_no
    else:
        return "PASS", max(ev_yes, ev_no)


def _legacy_kelly(p: float, price: float) -> float:
    if price <= 0.01 or price >= 0.99:
        return 0.0
    b = (1.0 / price) - 1.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


class TestDefaultsEquivalence:
    """Default arguments must reproduce the pre-change math bit-for-bit."""

    def test_expected_value_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert expected_value(p, price) == _legacy_ev(p, price)

    def test_expected_value_no_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert expected_value_no(p, price) == _legacy_ev_no(p, price)

    def test_best_trade_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert best_trade(p, price) == _legacy_best_trade(p, price)

    def test_kelly_fraction_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert kelly_fraction(p, price) == _legacy_kelly(p, price)

    def test_half_kelly_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert half_kelly(p, price) == _legacy_kelly(p, price) * 0.5

    def test_kelly_for_no_grid(self):
        for p in P_GRID:
            for price in PRICE_GRID:
                assert kelly_for_no(p, price) == _legacy_kelly(1.0 - p, 1.0 - price)

    def test_zero_frictions_reduce_to_legacy_formula(self):
        carry = FixedDailyCarry(0.005)
        for p in [0.3, 0.55, 0.7]:
            for price in [0.2, 0.4, 0.6]:
                assert kelly_fraction(p, price, holding_days=0.0) == _legacy_kelly(p, price)
                assert kelly_fraction(
                    p, price, carry_model=carry, holding_days=0.0
                ) == _legacy_kelly(p, price)


class TestFeeEffects:
    def test_fee_reduces_ev(self):
        fm = FlatCommissionFeeModel(0.01)
        assert expected_value(0.6, 0.4, fee_model=fm) < expected_value(0.6, 0.4)
        assert expected_value_no(0.2, 0.6, fee_model=fm) < expected_value_no(0.2, 0.6)

    def test_fee_reduces_kelly(self):
        fm = FlatCommissionFeeModel(0.01)
        assert kelly_fraction(0.6, 0.4, fee_model=fm) < kelly_fraction(0.6, 0.4)
        assert kelly_for_no(0.3, 0.6, fee_model=fm) < kelly_for_no(0.3, 0.6)

    def test_ev_closed_form_with_fee(self):
        # EV = 0.6*0.6 - 0.4*0.4 - 0.01 = 0.19
        ev = expected_value(0.6, 0.4, fee_model=FlatCommissionFeeModel(0.01))
        assert ev == pytest.approx(0.19, abs=1e-12)

    def test_kelly_closed_form_with_fee(self):
        # c = 0.51, r = 0: f = 0.75 - 0.25*0.51/0.49 = 24/49
        f = kelly_fraction(0.75, 0.5, fee_model=FlatCommissionFeeModel(0.01))
        assert f == pytest.approx(24.0 / 49.0, rel=1e-12)

    def test_heavy_fee_flips_best_trade_to_pass(self):
        direction, _ = best_trade(0.52, 0.5)
        assert direction == "YES"
        direction, ev = best_trade(0.52, 0.5, fee_model=FlatCommissionFeeModel(0.05))
        assert direction == "PASS"
        assert ev < 0

    def test_no_price_uses_venue_price_sum(self):
        # YES+NO = 1.02, YES at 0.60 -> NO at 0.42
        fm = FlatCommissionFeeModel(0.0, price_sum=1.02)
        ev = expected_value_no(0.5, 0.6, fee_model=fm)
        assert ev == pytest.approx(0.5 * (1.0 - 0.42) - 0.5 * 0.42, abs=1e-12)
        assert kelly_for_no(0.5, 0.6, fee_model=fm) == kelly_fraction(
            0.5, fm.no_price_from_yes(0.6), fee_model=fm
        )

    def test_effective_cost_guard(self):
        # Raw price passes the bounds guard but c = 0.95 + 0.05 >= 0.99
        assert kelly_fraction(0.999, 0.95, fee_model=FlatCommissionFeeModel(0.05)) == 0.0

    def test_raw_price_guard_still_applies(self):
        assert kelly_fraction(0.9, 0.995, fee_model=FlatCommissionFeeModel(0.01)) == 0.0
        assert kelly_fraction(0.9, 0.005, fee_model=FlatCommissionFeeModel(0.01)) == 0.0


class TestCarryEffects:
    def test_carry_increases_ev_monotonically(self):
        carry = ForecastExCouponCarry(apy=0.0314, verified_on=date.today())
        days = [0.0, 5.0, 20.0, 60.0, 180.0]
        evs = [expected_value(0.5, 0.5, carry_model=carry, holding_days=d) for d in days]
        assert evs[0] == expected_value(0.5, 0.5)
        for shorter, longer in zip(evs, evs[1:]):
            assert longer > shorter

    def test_carry_increases_kelly_monotonically(self):
        carry = ForecastExCouponCarry(apy=0.0314, verified_on=date.today())
        days = [0.0, 10.0, 40.0, 120.0, 365.0]
        fs = [kelly_fraction(0.6, 0.4, carry_model=carry, holding_days=d) for d in days]
        assert fs[0] == kelly_fraction(0.6, 0.4)
        for shorter, longer in zip(fs, fs[1:]):
            assert longer > shorter

    def test_ev_closed_form_with_carry(self):
        carry = FixedDailyCarry(0.001)
        # accrual = 0.4 * 0.001 * 10 = 0.004 on top of the base EV
        ev = expected_value(0.6, 0.4, carry_model=carry, holding_days=10.0)
        assert ev == pytest.approx(_legacy_ev(0.6, 0.4) + 0.004, abs=1e-12)

    def test_kelly_closed_form_with_carry(self):
        carry = FixedDailyCarry(0.0025)
        r = carry.accrual(0.4, 10.0)  # 0.01
        # c = 0.4: f = p*c/(c-r) - q*c/(1+r-c)
        expected = 0.6 * 0.4 / (0.4 - r) - 0.4 * 0.4 / (1.0 + r - 0.4)
        f = kelly_fraction(0.6, 0.4, carry_model=carry, holding_days=10.0)
        assert f == pytest.approx(expected, rel=1e-12)
        assert f == pytest.approx(0.24 / 0.39 - 0.16 / 0.61, rel=1e-9)

    def test_carry_covering_stake_clamps_to_one(self):
        # r = 0.4 * 0.2 * 10 = 0.8 >= c = 0.4: loss branch non-negative
        carry = FixedDailyCarry(0.2)
        assert kelly_fraction(0.6, 0.4, carry_model=carry, holding_days=10.0) == 1.0

    def test_negative_holding_days_accrue_nothing(self):
        carry = FixedDailyCarry(0.001)
        assert expected_value(0.6, 0.4, carry_model=carry, holding_days=-5.0) == expected_value(
            0.6, 0.4
        )


class TestNoComplement:
    def test_kelly_for_no_complement(self):
        fm = FlatCommissionFeeModel(0.01, price_sum=1.0)
        got = kelly_for_no(0.3, 0.6, fee_model=fm)
        assert got == kelly_fraction(1.0 - 0.3, 1.0 - 0.6, fee_model=fm)
        # sanity: c = 0.41, f = 0.7 - 0.3*0.41/0.59
        assert got == pytest.approx(0.7 - 0.3 * 0.41 / 0.59, rel=1e-9)


class TestThreading:
    def test_half_kelly_threads_kwargs(self):
        fm = FlatCommissionFeeModel(0.01)
        carry = FixedDailyCarry(0.001)
        full = kelly_fraction(0.65, 0.4, fee_model=fm, carry_model=carry, holding_days=20.0)
        half = half_kelly(0.65, 0.4, fee_model=fm, carry_model=carry, holding_days=20.0)
        assert half == full * 0.5

    def test_empirical_kelly_capped_by_half_kelly(self):
        fm = FlatCommissionFeeModel(0.01)
        carry = FixedDailyCarry(0.001)
        base = half_kelly(0.65, 0.4, fee_model=fm, carry_model=carry, holding_days=15.0)
        emp = empirical_kelly(
            0.65,
            0.4,
            confidence=0.9,
            n_simulations=2_000,
            fee_model=fm,
            carry_model=carry,
            holding_days=15.0,
        )
        assert 0.0 <= emp <= base

    def test_position_size_carry_increases_allocation(self):
        base = position_size(10_000, 0.6, 0.4, use_empirical=False, max_fraction=1.0)
        with_carry = position_size(
            10_000,
            0.6,
            0.4,
            use_empirical=False,
            max_fraction=1.0,
            carry_model=FixedDailyCarry(0.001),
            holding_days=30.0,
        )
        assert with_carry > base

    def test_position_size_fee_reduces_allocation(self):
        base = position_size(10_000, 0.6, 0.4, use_empirical=False, max_fraction=1.0)
        with_fee = position_size(
            10_000,
            0.6,
            0.4,
            use_empirical=False,
            max_fraction=1.0,
            fee_model=FlatCommissionFeeModel(0.01),
        )
        assert 0.0 < with_fee < base
