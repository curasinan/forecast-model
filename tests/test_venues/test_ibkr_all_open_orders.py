"""IBKR adapter cross-client open-order fetch: get_all_open_orders() + venue_meta."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.venues.ibkr.adapter import IBKRAdapter
from src.venues.ibkr.settings import IBKRSettings


@pytest.fixture(autouse=True)
def _clean_ibkr_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IBKR_"):
            monkeypatch.delenv(key)


def make_settings(**kwargs) -> IBKRSettings:
    kwargs.setdefault("port", 4002)
    kwargs.setdefault("trading_mode", "paper")
    return IBKRSettings(_env_file=None, **kwargs)


class FakeIB:
    def __init__(self, accounts=("DU1234567",)):
        self._accounts = accounts
        self._trades: list = []
        self._foreign_trades: list = []

    def isConnected(self):
        return True

    def managedAccounts(self):
        return self._accounts

    def reqCurrentTime(self):
        return 0

    def disconnect(self):
        ...

    def sleep(self, _s):
        ...

    def _make_trade(self, order_id, perm_id):
        contract = SimpleNamespace(conId=712856754, exchange="FORECASTX", right="C")
        order = SimpleNamespace(
            orderId=order_id, permId=perm_id, action="BUY", totalQuantity=3.0,
            orderType="LMT", lmtPrice=0.47, tif="GTC",
        )
        return SimpleNamespace(
            contract=contract, order=order,
            orderStatus=SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0),
        )

    def seed_own_trade(self, order_id, perm_id):
        trade = self._make_trade(order_id, perm_id)
        self._trades.append(trade)
        return trade

    def seed_foreign_trade(self, perm_id):
        trade = self._make_trade(0, perm_id)
        self._foreign_trades.append(trade)
        return trade

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]

    def reqAllOpenOrders(self):
        return list(self._trades) + list(self._foreign_trades)


def adapter_with_fake_ib() -> tuple[IBKRAdapter, FakeIB]:
    adapter = IBKRAdapter(make_settings(), role="paper_sync", read_only=True)
    fake = FakeIB()
    adapter.ib = fake
    adapter.connect()
    return adapter, fake


@pytest.fixture
def fake_adapter_with_foreign_trade():
    adapter, fake = adapter_with_fake_ib()
    fake.seed_foreign_trade(perm_id=987654321)
    return adapter


@pytest.fixture
def fake_adapter_with_own_trade():
    adapter, fake = adapter_with_fake_ib()
    fake.seed_own_trade(order_id=16, perm_id=111222333)
    return adapter


def test_all_open_orders_carries_perm_id(fake_adapter_with_foreign_trade):
    orders = fake_adapter_with_foreign_trade.get_all_open_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.venue_meta["perm_id"] == 987654321
    assert o.request.tif == "GTC" and o.request.side == "BUY"


def test_get_open_orders_also_fills_venue_meta(fake_adapter_with_own_trade):
    o = fake_adapter_with_own_trade.get_open_orders()[0]
    assert o.venue_meta["perm_id"] == 111222333
