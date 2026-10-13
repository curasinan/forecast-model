"""IBKR adapter order path: paper-only transmission, ack mapping, get_order."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.domain.ids import MarketId
from src.domain.models import OrderRequest
from src.venues.base import VenueError, VenueReadOnlyError
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
    def __init__(self, accounts, status="Submitted", qualify_ok=True):
        self._accounts = accounts
        self._status = status
        self._qualify_ok = qualify_ok
        self.placed: list[tuple] = []
        self._trades: list = []
        self._next_id = 100

    def isConnected(self): return True
    def managedAccounts(self): return self._accounts
    def reqCurrentTime(self): return 0
    def disconnect(self): ...
    def sleep(self, _s): ...

    def qualifyContracts(self, *contracts):
        if not self._qualify_ok:
            return []
        for c in contracts:
            c.secType = "OPT"
            c.right = "C"
            c.symbol = "GTM"
        return list(contracts)

    def placeOrder(self, contract, order):
        self._next_id += 1
        order.orderId = self._next_id
        order.permId = 555
        self.placed.append((contract, order))
        trade = SimpleNamespace(
            contract=contract, order=order,
            orderStatus=SimpleNamespace(status=self._status, filled=0.0, avgFillPrice=0.0),
        )
        self._trades.append(trade)
        return trade

    def trades(self): return list(self._trades)
    def openTrades(self): return [t for t in self._trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]

    # test helper
    def fill_all(self, price=0.49):
        for t in self._trades:
            t.orderStatus.status = "Filled"
            t.orderStatus.filled = t.order.totalQuantity
            t.orderStatus.avgFillPrice = price


def writable_adapter(accounts, **ib_kwargs) -> tuple[IBKRAdapter, FakeIB]:
    mode = "paper" if accounts[0].startswith(("DU", "DF")) else "live"
    adapter = IBKRAdapter(make_settings(trading_mode=mode), role="live_trading", read_only=False)
    fake = FakeIB(accounts, **ib_kwargs)
    adapter.ib = fake
    adapter.connect()  # verifies mode from the account prefix
    return adapter, fake


def req(**kw) -> OrderRequest:
    base = dict(market_id=MarketId.ibkr(712856754), side="BUY", leg="YES",
                quantity=3, order_type="LMT", limit_price=0.47, tif="GTC")
    base.update(kw)
    return OrderRequest(**base)


class TestPlaceOrderGating:
    def test_read_only_adapter_refuses_before_any_ib_call(self):
        adapter = IBKRAdapter(make_settings(), role="research", read_only=True)
        with pytest.raises(VenueReadOnlyError):
            adapter.place_order(req())

    def test_live_account_never_transmits(self):
        adapter, fake = writable_adapter(["U0000000"])
        assert adapter.mode == "live"
        with pytest.raises(VenueError, match="disabled in code"):
            adapter.place_order(req())
        assert fake.placed == []

    def test_paper_account_transmits_limit_gtc(self):
        adapter, fake = writable_adapter(["DU1234567"])
        ack = adapter.place_order(req())
        assert ack.accepted is True and ack.order_id == "101" and ack.detail == "Submitted"
        contract, order = fake.placed[0]
        assert contract.conId == 712856754 and contract.exchange == "FORECASTX"
        assert order.action == "BUY" and order.totalQuantity == 3
        assert order.orderType == "LMT" and order.lmtPrice == pytest.approx(0.47)
        assert order.tif == "GTC"  # explicit tif -- error 10349 otherwise
        assert ack.raw["mode"] == "paper" and ack.raw["account"] == "DU1234567"

    def test_paper_market_order_carries_explicit_tif(self):
        adapter, fake = writable_adapter(["DU1234567"])
        ack = adapter.place_order(req(order_type="MKT", limit_price=None, tif="DAY"))
        assert ack.accepted
        _, order = fake.placed[0]
        assert order.orderType == "MKT" and order.tif == "DAY"

    def test_inactive_status_is_not_accepted(self):
        adapter, _ = writable_adapter(["DU1234567"], status="Inactive")
        ack = adapter.place_order(req())
        assert ack.accepted is False and ack.detail == "Inactive"

    def test_unqualified_contract_is_refused_without_transmit(self):
        adapter, fake = writable_adapter(["DU1234567"], qualify_ok=False)
        ack = adapter.place_order(req())
        assert ack.accepted is False and "qualify" in ack.detail
        assert fake.placed == []

    def test_limit_without_price_is_refused(self):
        adapter, fake = writable_adapter(["DU1234567"])
        ack = adapter.place_order(req(limit_price=None))
        assert ack.accepted is False and fake.placed == []


class TestOrderLookup:
    def test_get_order_reflects_fill(self):
        adapter, fake = writable_adapter(["DU1234567"])
        ack = adapter.place_order(req())
        state = adapter.get_order(ack.order_id)
        assert state is not None and state.status == "open" and state.filled_quantity == 0
        fake.fill_all(price=0.46)
        state = adapter.get_order(ack.order_id)
        assert state.status == "filled" and state.filled_quantity == 3
        assert state.avg_fill_price == pytest.approx(0.46)
        assert state.request.market_id == MarketId.ibkr(712856754)

    def test_get_order_unknown_returns_none(self):
        adapter, _ = writable_adapter(["DU1234567"])
        assert adapter.get_order("999") is None
