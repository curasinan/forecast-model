"""The Polymarket live write path is retired; the dry-run simulation still works."""

import pytest

from src.domain.ids import MarketId
from src.domain.models import Market
from src.models.prediction import EdgeSignal
from src.trading.order_manager import OrderManager, OrderStatus, edge_to_order
from src.trading.wallet import PolymarketWallet
from src.venues.polymarket import PolymarketAdapter

RETIRED_MATCH = "retired to legacy/polymarket_onchain"


def _edge(question_id: str = "q-test", edge_pct: float = 0.10) -> EdgeSignal:
    return EdgeSignal(
        question_id=question_id,
        model_prob=0.60,
        market_prob=0.50,
        edge_pct=edge_pct,
        expected_value=0.10,
        kelly_fraction=0.05,
        confidence=0.8,
    )


def _market() -> Market:
    return Market(market_id=MarketId.polymarket("0xabc"), title="test market")


class TestDryRunStillWorks:
    def test_connect_returns_true(self):
        wallet = PolymarketWallet(dry_run=True)
        assert wallet.connect() is True

    def test_simulate_trade_debits_balance(self):
        wallet = PolymarketWallet(dry_run=True)
        wallet.connect()
        assert wallet.simulate_trade(side="BUY", price=0.5, size=20.0) is True
        assert wallet.get_balance().usdce == pytest.approx(1000.0 - 10.0)

    def test_get_client_returns_none_in_dry_run(self):
        wallet = PolymarketWallet(dry_run=True)
        assert wallet.get_client() is None

    def test_place_order_simulates_end_to_end_via_venue(self):
        # The Polymarket adapter is read-only, so the manager always simulates.
        manager = OrderManager(PolymarketAdapter(), sim_bankroll=1000.0)
        assert manager.is_simulated is True
        order = edge_to_order(edge=_edge(), size_usd=25.0, market=_market())
        assert manager.place_order(order) is True
        assert order.status == OrderStatus.SIMULATED
        assert order.filled_at is not None
        assert manager.simulated_balance == pytest.approx(1000.0 - 25.0)
        assert manager.get_trade_summary()["filled"] == 1

    def test_cancel_simulated_order(self):
        manager = OrderManager(PolymarketAdapter())
        order = edge_to_order(edge=_edge(), size_usd=10.0, market=_market())
        manager.place_order(order)
        assert manager.cancel_order(order.order_id) is True
        assert order.status == OrderStatus.CANCELLED


class TestLivePathRetired:
    def test_live_connect_raises(self):
        wallet = PolymarketWallet(private_key="0xdeadbeef", dry_run=False)
        with pytest.raises(RuntimeError, match=RETIRED_MATCH):
            wallet.connect()

    def test_live_get_client_raises(self):
        wallet = PolymarketWallet(private_key="0xdeadbeef", dry_run=False)
        with pytest.raises(RuntimeError, match=RETIRED_MATCH):
            wallet.get_client()

    def test_polymarket_adapter_never_transmits(self):
        manager = OrderManager(PolymarketAdapter(), simulate=False)
        assert manager.is_simulated is True
        order = edge_to_order(edge=_edge(), size_usd=25.0, market=_market())
        assert manager.place_order(order) is True
        assert order.status == OrderStatus.SIMULATED
