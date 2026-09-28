"""TradingExecutor / OrderManager on the VenueAdapter port (no venue libs)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.carry import CarryModel, ForecastExCouponCarry, NullCarry
from src.domain.fees import FeeModel, FlatCommissionFeeModel, ZeroFeeModel
from src.domain.ids import MarketId
from src.domain.models import (
    Balance,
    Market,
    Order,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
    Quote,
)
from src.models.prediction import EdgeSignal
from src.trading.executor import TradeIntent, TradingExecutor
from src.trading.order_manager import (
    OrderManager,
    OrderStatus,
    edge_to_order,
    snap_to_tick,
)
from src.trading.risk_manager import RiskLimits
from src.venues.base import VenueAdapter, VenueError, VenueReadOnlyError


class FakeVenue(VenueAdapter):
    """In-memory venue: accepts orders, can be told to fill/cancel them."""

    name = "fake"

    def __init__(self, read_only: bool = True, fee: FeeModel | None = None,
                 carry: CarryModel | None = None, accept: bool = True):
        super().__init__(read_only=read_only)
        self._fee = fee or ZeroFeeModel()
        self._carry = carry or NullCarry()
        self._accept = accept
        self.requests: list[OrderRequest] = []
        self._orders: dict[str, Order] = {}
        self._n = 0

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_healthy(self) -> bool: return True
    def list_markets(self) -> list[Market]: return []
    def get_market(self, market_id: MarketId) -> Market: return Market(market_id=market_id)
    def get_order_book(self, market_id: MarketId, depth: int = 5) -> OrderBook:
        return OrderBook(market_id=market_id, ts=datetime.now(timezone.utc))
    def get_quotes(self, market_ids) -> list[Quote]:
        return [Quote(market_id=m, bid=0.4, ask=0.42, ts=datetime.now(timezone.utc)) for m in market_ids]
    def get_positions(self) -> list[Position]: return []
    def get_balances(self) -> list[Balance]: return [Balance(currency="USD", total=100.0, available=100.0)]
    def get_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status in ("pending", "open")]
    def get_order(self, order_id: str) -> Order | None: return self._orders.get(order_id)
    def cancel_order(self, order_id: str) -> bool:
        o = self._orders.get(order_id)
        if not o:
            return False
        self._orders[order_id] = o.model_copy(update={"status": "cancelled"})
        return True
    def fee_model(self) -> FeeModel: return self._fee
    def carry_model(self) -> CarryModel: return self._carry

    def _place_order(self, request: OrderRequest) -> OrderAck:
        self.requests.append(request)
        self._n += 1
        oid = f"V{self._n}"
        if not self._accept:
            return OrderAck(order_id=oid, accepted=False, detail="Inactive")
        self._orders[oid] = Order(order_id=oid, request=request, status="open")
        return OrderAck(order_id=oid, accepted=True, detail="Submitted")

    # test helper
    def fill(self, oid: str, price: float | None = None):
        o = self._orders[oid]
        self._orders[oid] = o.model_copy(update={
            "status": "filled", "filled_quantity": o.request.quantity,
            "avg_fill_price": price if price is not None else o.request.limit_price,
        })


def _edge(edge_pct=0.15, model_prob=0.65, market_prob=0.50, confidence=0.8) -> EdgeSignal:
    return EdgeSignal(question_id="gtm-2026-12-1.2", model_prob=model_prob, market_prob=market_prob,
                      edge_pct=edge_pct, expected_value=0.15, kelly_fraction=0.10,
                      confidence=confidence)


def _ibkr_market(days: int = 120) -> Market:
    return Market(
        market_id=MarketId.ibkr(712856754), no_id=MarketId.ibkr(712856759),
        symbol="GTM", title="GTM Dec-2026 > 1.2", strike=1.2, size_increment=1.0,
        tick_size=0.01,
        scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=days),
    )


# ---------------------------------------------------------------- edge_to_order
class TestEdgeToOrder:
    def test_yes_leg_targets_market_id_and_rounds_to_whole_contracts(self):
        o = edge_to_order(_edge(), size_usd=25.0, market=_ibkr_market())
        assert o.outcome == "YES" and o.market_id == "ibkr:712856754"
        assert o.size == 50.0 and o.cost_usd == pytest.approx(25.0)  # 25/0.5 = 50 whole contracts
        assert o.venue == "ibkr"

    def test_yes_leg_rounds_down_not_up(self):
        o = edge_to_order(_edge(market_prob=0.33, edge_pct=0.2), size_usd=10.0, market=_ibkr_market())
        assert o.size == 30.0  # 10/0.33 = 30.30 -> 30
        assert o.cost_usd == pytest.approx(30 * 0.33, abs=1e-2)

    def test_no_leg_uses_no_quote_when_given(self):
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=_ibkr_market(), no_price=0.53)
        assert o.outcome == "NO" and o.market_id == "ibkr:712856759"
        assert o.price == pytest.approx(0.53)

    def test_no_leg_falls_back_to_fee_model_complement(self):
        fee = FlatCommissionFeeModel(0.01, price_sum=1.0)
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=_ibkr_market(), fee_model=fee)
        assert o.price == pytest.approx(fee.no_price_from_yes(0.5))

    def test_off_tick_price_is_snapped_down_to_the_venue_tick(self):
        # 0.345 is a legal mid but not a legal ForecastEx price: the venue
        # CANCELS such an order (error 110, verified on paper 2026-08-25).
        o = edge_to_order(_edge(market_prob=0.345, edge_pct=0.2), size_usd=10.0,
                          market=_ibkr_market())
        assert o.price == pytest.approx(0.34)
        assert o.size == 29.0  # 10/0.34 = 29.41 -> 29 whole contracts
        assert o.cost_usd == pytest.approx(29 * 0.34, abs=1e-2)

    def test_no_leg_price_is_snapped_too(self):
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=_ibkr_market(), no_price=0.6549)
        assert o.outcome == "NO" and o.price == pytest.approx(0.65)

    def test_market_without_a_tick_size_keeps_the_raw_price(self):
        m = Market(market_id=MarketId.polymarket("0xabc"))  # tick_size None
        o = edge_to_order(_edge(market_prob=0.345, edge_pct=0.2), size_usd=10.0, market=m)
        assert o.price == pytest.approx(0.345)

    def test_fractional_venue_keeps_fractional_size(self):
        m = Market(market_id=MarketId.polymarket("0xabc"))  # size_increment None
        o = edge_to_order(_edge(market_prob=0.33, edge_pct=0.2), size_usd=10.0, market=m)
        assert o.size == pytest.approx(30.303, abs=1e-3)

    def test_no_leg_on_ibkr_market_missing_no_id_yields_empty_leg_id(self):
        # M8 (Task 3.8): an IBKR market published without its NO contract's
        # conId must never fall back to the YES market_id -- that would buy
        # the WRONG leg at the NO price. Empty leg_id is the primary fence;
        # OrderManager.place_order's "no market id for the leg" rejection
        # (checked before BOTH the simulate and venue paths, I-1 fix round
        # 1) is what actually stops the order (see TestOrderManager below).
        m = Market(market_id=MarketId.ibkr(712856754))  # no no_id
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=m, no_price=0.52)
        assert o.outcome == "NO" and o.market_id == ""

    def test_no_leg_on_polymarket_still_falls_back_to_market_id(self):
        # The M8 guard is IBKR-specific: Polymarket's single-contract venue
        # never publishes a separate no_id by design, so the market_id
        # fallback must survive the guard unchanged.
        m = Market(market_id=MarketId.polymarket("0xabc"))  # no no_id
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=m, no_price=0.52)
        assert o.outcome == "NO" and o.market_id == "polymarket:0xabc"


# ---------------------------------------------------------------- OrderManager
class TestOrderManager:
    def test_read_only_venue_always_simulates(self):
        om = OrderManager(FakeVenue(read_only=True), simulate=False)
        assert om.is_simulated is True

    def test_writable_venue_places_and_syncs_fill(self):
        venue = FakeVenue(read_only=False)
        om = OrderManager(venue, simulate=False)
        o = edge_to_order(_edge(), size_usd=25.0, market=_ibkr_market())
        assert om.place_order(o) is True
        assert o.status == OrderStatus.PLACED and o.venue_order_id == "V1"
        req = venue.requests[0]
        assert req.tif == "GTC" and req.order_type == "LMT" and req.leg == "YES"
        assert req.market_id == MarketId.ibkr(712856754) and req.quantity == 50.0
        # nothing filled yet
        assert om.sync_with_venue() == 0 and o.status == OrderStatus.PLACED
        venue.fill("V1", price=0.49)
        assert om.sync_with_venue() == 1
        assert o.status == OrderStatus.FILLED and o.price == pytest.approx(0.49)
        assert o.cost_usd == pytest.approx(0.49 * 50)

    def test_venue_rejection_is_recorded_not_raised(self):
        om = OrderManager(FakeVenue(read_only=False, accept=False), simulate=False)
        o = edge_to_order(_edge(), size_usd=25.0, market=_ibkr_market())
        assert om.place_order(o) is False
        assert o.status == OrderStatus.REJECTED and "Inactive" in o.reason

    def test_venue_error_is_recorded_not_raised(self):
        class Refusing(FakeVenue):
            def _place_order(self, request):
                raise VenueError("live transmission disabled")
        om = OrderManager(Refusing(read_only=False), simulate=False)
        o = edge_to_order(_edge(), size_usd=25.0, market=_ibkr_market())
        assert om.place_order(o) is False
        assert "disabled" in o.reason

    def test_zero_size_after_rounding_is_rejected(self):
        o = edge_to_order(_edge(), size_usd=0.2, market=_ibkr_market())  # 0.4 contracts -> 0
        om = OrderManager(FakeVenue(read_only=False), simulate=False)
        assert om.place_order(o) is False and o.status == OrderStatus.REJECTED

    def test_cancel_routes_to_venue(self):
        venue = FakeVenue(read_only=False)
        om = OrderManager(venue, simulate=False)
        o = edge_to_order(_edge(), size_usd=25.0, market=_ibkr_market())
        om.place_order(o)
        assert om.cancel_order(o.order_id) is True and o.status == OrderStatus.CANCELLED

    def test_base_place_order_refuses_when_read_only(self):
        venue = FakeVenue(read_only=True)
        req = OrderRequest(market_id=MarketId.ibkr(1), side="BUY", leg="YES", quantity=1,
                           limit_price=0.5, tif="GTC")
        with pytest.raises(VenueReadOnlyError):
            venue.place_order(req)

    def test_no_leg_without_no_id_is_rejected_and_venue_receives_nothing(self):
        # M8 (Task 3.8): the primary fence against buying the wrong leg. The
        # dedup drop in run_ibkr_paper.py (Task 1.7) is the belt; this is
        # the buckle -- even if a no_id=None intent slipped past dedup, the
        # order must be REJECTED here and the venue must never see a request.
        m = Market(market_id=MarketId.ibkr(712856754))  # no no_id
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=m, no_price=0.52)
        venue = FakeVenue(read_only=False)
        om = OrderManager(venue, simulate=False)
        assert om.place_order(o) is False
        assert o.status == OrderStatus.REJECTED
        assert "no market id for the leg" in o.reason
        assert venue.requests == []

    def test_no_leg_without_no_id_is_rejected_in_simulate_mode_too(self):
        # I-1 (Task 3.8 fix round 1): the empty-leg guard used to live only
        # in _place_venue_order, so simulate=True (or any read-only venue,
        # which always simulates -- see test_read_only_venue_always_simulates
        # above) routed straight to _simulate_order, which never inspects
        # market_id -- fabricating a SIMULATED fill on a leg that doesn't
        # exist. place_order must reject BEFORE routing to either path.
        m = Market(market_id=MarketId.ibkr(712856754))  # no no_id
        o = edge_to_order(_edge(edge_pct=-0.15, model_prob=0.35), size_usd=10.0,
                          market=m, no_price=0.52)
        om = OrderManager(FakeVenue(read_only=True), simulate=True)
        assert om.is_simulated is True
        assert om.place_order(o) is False
        assert o.status == OrderStatus.REJECTED
        assert "no market id for the leg" in o.reason
        assert o.filled_size == 0.0  # no fabricated fill
        assert om.simulated_balance == pytest.approx(1000.0)  # no debit either


# ---------------------------------------------------------------- Executor
class TestExecutor:
    def _executor(self, venue, tmp_path, **kw):
        return TradingExecutor(venue=venue, bankroll=1000.0, db_path=str(tmp_path / "t.db"),
                               risk_limits=RiskLimits(max_position_usd=50.0), **kw)

    def test_simulated_run_records_position_and_exposure(self, tmp_path):
        ex = self._executor(FakeVenue(read_only=True), tmp_path)
        rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market(), title="GTM")],
                                 mode="auto")
        assert rep.simulated is True and rep.trades_executed == 1
        d = rep.decisions[0]
        assert d.order.status == OrderStatus.SIMULATED and d.holding_days == pytest.approx(120, abs=1)
        assert ex.risk_manager._total_exposure == pytest.approx(d.order.cost_usd)
        summary = ex.position_tracker.get_portfolio_summary()
        assert summary["open_positions"] == 1

    def test_fee_and_carry_reprice_the_edge(self, tmp_path):
        plain = FakeVenue(read_only=True)
        costly = FakeVenue(read_only=True, fee=FlatCommissionFeeModel(0.05, price_sum=1.0))
        carry = FakeVenue(read_only=True,
                          carry=ForecastExCouponCarry(apy=0.0314, verified_on=date.today()))
        d0 = self._executor(plain, tmp_path).execute_intents(
            [TradeIntent(edge=_edge(), market=_ibkr_market(days=365))], mode="auto").decisions[0]
        d1 = self._executor(costly, tmp_path).execute_intents(
            [TradeIntent(edge=_edge(), market=_ibkr_market(days=365))], mode="auto").decisions[0]
        d2 = self._executor(carry, tmp_path).execute_intents(
            [TradeIntent(edge=_edge(), market=_ibkr_market(days=365))], mode="auto").decisions[0]
        assert d1.net_expected_value < d0.net_expected_value  # fee lowers EV
        assert d1.net_kelly_fraction < d0.net_kelly_fraction
        assert d2.net_expected_value > d0.net_expected_value  # carry over a year raises EV
        assert d2.net_kelly_fraction > d0.net_kelly_fraction

    def test_venue_path_places_then_sync_records_fill(self, tmp_path):
        venue = FakeVenue(read_only=False)
        ex = self._executor(venue, tmp_path, simulate=False)
        assert ex.is_simulated is False
        rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
        d = rep.decisions[0]
        assert d.executed and d.order.status == OrderStatus.PLACED
        # PLACED is not a position yet
        assert ex.position_tracker.get_portfolio_summary()["open_positions"] == 0
        venue.fill(d.order.venue_order_id)
        assert ex.sync() == 1
        assert d.order.status == OrderStatus.FILLED
        assert ex.position_tracker.get_portfolio_summary()["open_positions"] == 1
        ex.sync()  # idempotent: no duplicate position
        assert ex.position_tracker.get_portfolio_summary()["open_positions"] == 1

    def test_approve_mode_respects_rejection(self, tmp_path):
        ex = self._executor(FakeVenue(), tmp_path, approval_input=lambda _p: "n")
        rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="approve")
        assert rep.trades_executed == 0 and rep.decisions[0].reason == "User rejected"

    def test_no_edge_is_skipped_without_risk_checks(self, tmp_path):
        ex = self._executor(FakeVenue(), tmp_path)
        weak = _edge(edge_pct=0.01, model_prob=0.51)
        rep = ex.execute_intents([TradeIntent(edge=weak, market=_ibkr_market())], mode="auto")
        assert rep.trades_rejected == 1 and rep.decisions[0].reason == "No edge detected"

    def test_no_direction_orders_the_no_leg(self, tmp_path):
        ex = self._executor(FakeVenue(), tmp_path)
        e = _edge(edge_pct=-0.15, model_prob=0.35)
        rep = ex.execute_intents([TradeIntent(edge=e, market=_ibkr_market(), no_price=0.52)], mode="auto")
        o = rep.decisions[0].order
        assert o is not None and o.outcome == "NO" and o.market_id == "ibkr:712856759"
        assert o.price == pytest.approx(0.52)

    def test_no_leg_without_no_id_is_rejected_end_to_end(self, tmp_path):
        # I-1 (Task 3.8 fix round 1): same fabricated-fill scenario as
        # TestOrderManager.test_no_leg_without_no_id_is_rejected_in_simulate_mode_too,
        # driven through the full executor pipeline. FakeVenue() defaults
        # read_only=True -> always simulated, exactly the path I-1 found
        # unguarded. Confirms no position, exposure, or group bookkeeping
        # happens for the rejected order.
        ex = self._executor(FakeVenue(), tmp_path)
        m = Market(
            market_id=MarketId.ibkr(712856754),  # no no_id -- NO leg unresolved
            symbol="GTM", title="GTM Dec-2026 > 1.2", strike=1.2, size_increment=1.0,
            tick_size=0.01,
            scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=120),
        )
        e = _edge(edge_pct=-0.15, model_prob=0.35)
        rep = ex.execute_intents([TradeIntent(edge=e, market=m, no_price=0.52)], mode="auto")
        d = rep.decisions[0]
        assert rep.trades_executed == 0 and rep.trades_rejected == 1
        assert d.order is not None and d.order.status == OrderStatus.REJECTED
        assert "no market id for the leg" in d.order.reason
        assert ex.risk_manager._total_exposure == 0.0
        assert ex.position_tracker.get_portfolio_summary()["open_positions"] == 0

    def test_min_edge_pct_gate_is_read_from_risk_limits_not_hardcoded(self, tmp_path):
        # L1 (Task 3.8): _evaluate's initial "no edge" skip used to hardcode
        # EdgeSignal.has_edge's 0.05 threshold, ignoring whatever
        # RiskLimits.min_edge_pct the caller configured -- check_trade's own
        # edge-threshold check (a few lines further down the pipeline) was
        # already re-sourced, so a caller who lowered min_edge_pct still got
        # silently blocked at this EARLIER gate. edge_pct=0.04 sits below
        # the has_edge/default-limits 0.05 floor but above a 0.03 override.
        weak = _edge(edge_pct=0.04, model_prob=0.54)

        ex_default = TradingExecutor(venue=FakeVenue(), bankroll=1000.0,
                                     db_path=str(tmp_path / "default.db"))
        rep_default = ex_default.execute_intents(
            [TradeIntent(edge=weak, market=_ibkr_market())], mode="auto")
        # Parity: default min_edge_pct (0.05) still blocks it, same as before L1.
        assert rep_default.trades_rejected == 1
        assert rep_default.decisions[0].reason == "No edge detected"

        ex_lower = TradingExecutor(venue=FakeVenue(), bankroll=1000.0,
                                   db_path=str(tmp_path / "lower.db"),
                                   risk_limits=RiskLimits(min_edge_pct=0.03))
        rep_lower = ex_lower.execute_intents(
            [TradeIntent(edge=weak, market=_ibkr_market())], mode="auto")
        # A configured 0.03 floor now lets it through to risk checks.
        assert rep_lower.trades_proposed == 1
        assert rep_lower.decisions[0].reason != "No edge detected"

    def test_min_edge_pct_gate_uses_strict_greater_than_at_the_boundary(self, tmp_path):
        # C-1 (Task 3.8 fix round 1, controller ruling R18): the brief's
        # literal ">=" loosened this gate relative to the has_edge property
        # it replaced (strict ">"), so an edge_pct landing EXACTLY on the
        # default 0.05 floor -- reachable in practice, e.g.
        # 0.10 - 0.05 == 0.05 in IEEE-754 -- was rejected before L1 and
        # would have been proposed after it. Ruling: parity wins, so
        # _evaluate must use strict ">" (matching has_edge), NOT ">="
        # (check_trade's own pre-existing edge check a few lines further
        # down the pipeline stays ">=" -- untouched, not this gate).
        edge_pct = 0.10 - 0.05
        assert edge_pct == 0.05  # exact in IEEE-754, not an approximation
        boundary = _edge(edge_pct=edge_pct, model_prob=0.55)

        ex = TradingExecutor(venue=FakeVenue(), bankroll=1000.0,
                             db_path=str(tmp_path / "boundary.db"))
        rep = ex.execute_intents(
            [TradeIntent(edge=boundary, market=_ibkr_market())], mode="auto")
        assert rep.trades_rejected == 1
        assert rep.decisions[0].reason == "No edge detected"


class TestSnapToTick:
    def test_exact_multiples_are_unchanged(self):
        assert snap_to_tick(0.35, 0.01) == pytest.approx(0.35)
        assert snap_to_tick(0.5, 0.01) == pytest.approx(0.5)

    def test_always_rounds_down_so_a_buy_never_pays_more(self):
        assert snap_to_tick(0.999, 0.01) == pytest.approx(0.99)
        assert snap_to_tick(0.341, 0.01) == pytest.approx(0.34)

    def test_sub_tick_price_floors_at_one_tick_not_zero(self):
        assert snap_to_tick(0.004, 0.01) == pytest.approx(0.01)

    def test_binary_float_artifacts_do_not_leak_into_the_price(self):
        # 0.7/0.1 is 6.999... in binary floats; Decimal keeps it exact.
        assert snap_to_tick(0.7, 0.1) == pytest.approx(0.7)
        assert str(snap_to_tick(0.35, 0.01)) == "0.35"

    def test_non_decimal_ticks_are_honoured(self):
        assert snap_to_tick(0.126, 0.005) == pytest.approx(0.125)
