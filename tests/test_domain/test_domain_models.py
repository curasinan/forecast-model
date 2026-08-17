"""Unit tests for the venue-neutral domain layer: ids, fees, carry, models."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.domain.carry import ForecastExCouponCarry, NullCarry, StaleCarryRateError
from src.domain.fees import FlatCommissionFeeModel, StaleFeeError, ZeroFeeModel
from src.domain.ids import MarketId
from src.domain.models import (
    Market,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    Quote,
)

TS = datetime(2026, 8, 16, 12, 0, 0)


class TestMarketId:
    def test_frozen(self) -> None:
        mid = MarketId(venue="ibkr", native_id="123")
        with pytest.raises(ValidationError):
            mid.venue = "polymarket"  # type: ignore[misc]

    def test_hashable_and_equality(self) -> None:
        a = MarketId(venue="ibkr", native_id="123")
        b = MarketId(venue="ibkr", native_id="123")
        c = MarketId(venue="polymarket", native_id="123")
        assert a == b
        assert hash(a) == hash(b)
        assert a != c
        assert len({a, b, c}) == 2

    def test_str_round_trip_via_parse(self) -> None:
        mid = MarketId(venue="polymarket", native_id="0xabc123")
        assert str(mid) == "polymarket:0xabc123"
        assert MarketId.parse(str(mid)) == mid

    def test_parse_keeps_colons_in_native_id(self) -> None:
        mid = MarketId.parse("ibkr:756:extra")
        assert mid.venue == "ibkr"
        assert mid.native_id == "756:extra"

    @pytest.mark.parametrize("text", ["", "no-separator", ":native", "venue:"])
    def test_parse_rejects_malformed(self, text: str) -> None:
        with pytest.raises(ValueError):
            MarketId.parse(text)

    def test_ibkr_constructor(self) -> None:
        mid = MarketId.ibkr(756733)
        assert mid.venue == "ibkr"
        assert mid.native_id == "756733"
        assert mid.con_id == 756733

    def test_polymarket_constructor(self) -> None:
        mid = MarketId.polymarket("0xdeadbeef")
        assert mid.venue == "polymarket"
        assert mid.native_id == "0xdeadbeef"

    def test_con_id_raises_for_non_ibkr(self) -> None:
        with pytest.raises(ValueError):
            _ = MarketId.polymarket("0xdeadbeef").con_id


class TestZeroFeeModel:
    def test_execution_cost_is_zero(self) -> None:
        model = ZeroFeeModel()
        assert model.execution_cost(0.55, 100.0) == 0.0

    def test_price_complement(self) -> None:
        model = ZeroFeeModel()
        assert model.yes_no_price_sum() == 1.0
        assert model.no_price_from_yes(0.62) == pytest.approx(0.38)


class TestFlatCommissionFeeModel:
    def test_execution_cost_flat_per_contract(self) -> None:
        model = FlatCommissionFeeModel(commission_per_contract=0.01)
        assert model.execution_cost(0.55, 100.0) == pytest.approx(1.0)
        assert model.execution_cost(0.55, -100.0) == pytest.approx(1.0)

    def test_price_complement_with_custom_sum(self) -> None:
        model = FlatCommissionFeeModel(commission_per_contract=0.01, price_sum=1.01)
        assert model.yes_no_price_sum() == pytest.approx(1.01)
        assert model.no_price_from_yes(0.62) == pytest.approx(0.39)

    def test_default_price_sum_is_one(self) -> None:
        model = FlatCommissionFeeModel(commission_per_contract=0.01)
        assert model.yes_no_price_sum() == 1.0

    def test_rejects_negative_commission(self) -> None:
        with pytest.raises(ValueError):
            FlatCommissionFeeModel(commission_per_contract=-0.01)

    def test_fresh_verification_accepted(self) -> None:
        model = FlatCommissionFeeModel(
            commission_per_contract=0.01, verified_on=date.today() - timedelta(days=30)
        )
        assert model.verified_on == date.today() - timedelta(days=30)

    def test_stale_verification_raises(self) -> None:
        with pytest.raises(StaleFeeError):
            FlatCommissionFeeModel(
                commission_per_contract=0.01, verified_on=date.today() - timedelta(days=31)
            )

    def test_custom_max_age(self) -> None:
        with pytest.raises(StaleFeeError):
            FlatCommissionFeeModel(
                commission_per_contract=0.01,
                verified_on=date.today() - timedelta(days=8),
                max_age_days=7,
            )


class TestNullCarry:
    def test_zero_accrual(self) -> None:
        carry = NullCarry()
        assert carry.daily_accrual_rate() == 0.0
        assert carry.accrual(10_000.0, 30.0) == 0.0


class TestForecastExCouponCarry:
    def test_accrual_math(self) -> None:
        carry = ForecastExCouponCarry(apy=0.0314, verified_on=date.today())
        assert carry.daily_accrual_rate() == pytest.approx(0.0314 / 365.0)
        assert carry.accrual(1000.0, 10.0) == pytest.approx(1000.0 * 0.0314 / 365.0 * 10.0)

    def test_negative_holding_period_clamps_to_zero(self) -> None:
        carry = ForecastExCouponCarry(apy=0.0314, verified_on=date.today())
        assert carry.accrual(1000.0, -5.0) == 0.0

    def test_stale_rate_raises(self) -> None:
        with pytest.raises(StaleCarryRateError):
            ForecastExCouponCarry(apy=0.0314, verified_on=date.today() - timedelta(days=31))

    @pytest.mark.parametrize("apy", [-0.01, 0.5, 1.0])
    def test_rejects_implausible_apy(self, apy: float) -> None:
        with pytest.raises(ValueError):
            ForecastExCouponCarry(apy=apy, verified_on=date.today())

    def test_zero_apy_allowed(self) -> None:
        carry = ForecastExCouponCarry(apy=0.0, verified_on=date.today())
        assert carry.daily_accrual_rate() == 0.0


class TestModels:
    def test_market_construction_defaults(self) -> None:
        market = Market(market_id=MarketId.ibkr(1))
        assert market.no_id is None
        assert market.multiplier == 1.0
        assert market.currency == "USD"
        assert market.venue_meta == {}

    def test_market_venue_meta_not_shared(self) -> None:
        a = Market(market_id=MarketId.ibkr(1))
        b = Market(market_id=MarketId.ibkr(2))
        a.venue_meta["k"] = "v"
        assert b.venue_meta == {}

    def test_quote_mid(self) -> None:
        quote = Quote(market_id=MarketId.ibkr(1), bid=0.40, ask=0.44, ts=TS)
        assert quote.mid == pytest.approx(0.42)

    def test_quote_mid_none_when_one_sided(self) -> None:
        assert Quote(market_id=MarketId.ibkr(1), bid=0.40, ts=TS).mid is None
        assert Quote(market_id=MarketId.ibkr(1), ask=0.44, ts=TS).mid is None

    def test_order_book_best_levels(self) -> None:
        book = OrderBook(
            market_id=MarketId.polymarket("0xabc"),
            bids=[OrderBookLevel(price=0.41, size=100.0), OrderBookLevel(price=0.40, size=50.0)],
            asks=[OrderBookLevel(price=0.43, size=80.0)],
            ts=TS,
        )
        assert book.best_bid == pytest.approx(0.41)
        assert book.best_ask == pytest.approx(0.43)

    def test_order_book_empty_sides(self) -> None:
        book = OrderBook(market_id=MarketId.ibkr(1), ts=TS)
        assert book.best_bid is None
        assert book.best_ask is None

    def test_order_request_frozen_and_defaults(self) -> None:
        req = OrderRequest(
            market_id=MarketId.ibkr(1), side="BUY", leg="YES", quantity=10.0, limit_price=0.42
        )
        assert req.order_type == "LMT"
        assert req.tif == "DAY"
        with pytest.raises(ValidationError):
            req.quantity = 20.0  # type: ignore[misc]
