"""The Polymarket live write path is retired; dry-run simulation still works."""

import pytest

from src.models.prediction import EdgeSignal
from src.trading.order_manager import OrderManager, OrderStatus, edge_to_order
from src.trading.wallet import PolymarketWallet

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


class TestDryRunStillWorks:
    def test_connect_returns_true(self):
        wallet = PolymarketWallet(dry_run=True)
        assert wallet.connect() is True
        assert wallet.is_connected
        assert wallet.is_dry_run

    def test_simulate_trade_updates_balance(self):
        wallet = PolymarketWallet(dry_run=True)
        wallet.connect()
        start = wallet.get_balance().usdce
        assert wallet.simulate_trade(side="BUY", price=0.5, size=100) is True
        assert wallet.get_balance().usdce == pytest.approx(start - 50.0)
        assert wallet.simulate_trade(side="SELL", price=0.5, size=100) is True
        assert wallet.get_balance().usdce == pytest.approx(start)

    def test_simulate_trade_insufficient_balance(self):
        wallet = PolymarketWallet(dry_run=True)
        wallet.connect()
        assert wallet.simulate_trade(side="BUY", price=0.5, size=1_000_000) is False

    def test_get_client_returns_none_in_dry_run(self):
        wallet = PolymarketWallet(dry_run=True)
        assert wallet.get_client() is None

    def test_place_order_simulates_end_to_end(self):
        wallet = PolymarketWallet(dry_run=True)
        wallet.connect()
        manager = OrderManager(wallet=wallet)

        order = edge_to_order(edge=_edge(), size_usd=25.0)
        assert manager.place_order(order) is True
        assert order.status == OrderStatus.SIMULATED
        assert order.filled_at is not None
        assert wallet.get_balance().usdce == pytest.approx(1000.0 - 25.0)
        assert manager.get_trade_summary()["filled"] == 1

    def test_cancel_simulated_order(self):
        wallet = PolymarketWallet(dry_run=True)
        wallet.connect()
        manager = OrderManager(wallet=wallet)
        order = edge_to_order(edge=_edge(), size_usd=10.0)
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

    def test_place_live_order_raises(self):
        wallet = PolymarketWallet(private_key="0xdeadbeef", dry_run=False)
        manager = OrderManager(wallet=wallet)
        order = edge_to_order(edge=_edge(), size_usd=25.0)
        with pytest.raises(RuntimeError, match=RETIRED_MATCH):
            manager._place_live_order(order)

    def test_place_order_routes_live_wallet_to_retired_path(self):
        wallet = PolymarketWallet(private_key="0xdeadbeef", dry_run=False)
        manager = OrderManager(wallet=wallet)
        order = edge_to_order(edge=_edge(), size_usd=25.0)
        with pytest.raises(RuntimeError, match=RETIRED_MATCH):
            manager.place_order(order)

    def test_cancel_live_order_raises(self):
        dry_wallet = PolymarketWallet(dry_run=True)
        dry_wallet.connect()
        manager = OrderManager(wallet=dry_wallet)
        order = edge_to_order(edge=_edge(), size_usd=10.0)
        manager.place_order(order)

        live_manager = OrderManager(wallet=PolymarketWallet(private_key="0x1", dry_run=False))
        live_manager._orders.append(order)
        with pytest.raises(RuntimeError, match=RETIRED_MATCH):
            live_manager.cancel_order(order.order_id)
