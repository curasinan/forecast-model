"""IBKR cancel_order waits for the venue's final state before saying True."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.venues.base import VenueReadOnlyError
from src.venues.ibkr.adapter import IBKRAdapter
from src.venues.ibkr.settings import IBKRSettings


@pytest.fixture(autouse=True)
def _clean_ibkr_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IBKR_"):
            monkeypatch.delenv(key)


class FakeIB:
    """Open trades whose status moves to `final` on cancel, after `delay_sleeps` sleeps."""

    def __init__(self, final: str = "Cancelled", delay_sleeps: int = 0):
        self.final = final
        self.delay_sleeps = delay_sleeps
        self.cancelled: list[int] = []
        self._pending: list = []
        self._trades = []

    def isConnected(self):
        return True

    def managedAccounts(self):
        return ["DU1234567"]

    def disconnect(self):
        ...

    def seed(self, order_id: int, perm_id: int):
        trade = SimpleNamespace(
            contract=SimpleNamespace(conId=712856754, right="C"),
            order=SimpleNamespace(orderId=order_id, permId=perm_id, action="BUY",
                                  totalQuantity=29.0, orderType="LMT", lmtPrice=0.34, tif="GTC"),
            orderStatus=SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0),
        )
        self._trades.append(trade)
        return trade

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        trade = next(t for t in self._trades if t.order is order)
        trade.orderStatus.status = "PendingCancel"
        self._pending.append([trade, self.delay_sleeps])
        self._advance()

    def sleep(self, _s):
        for item in self._pending:
            item[1] -= 1
        self._advance()

    def _advance(self):
        for trade, left in self._pending:
            if left <= 0 and self.final is not None:
                trade.orderStatus.status = self.final


def _adapter(fake) -> IBKRAdapter:
    settings = IBKRSettings(_env_file=None, port=4002, trading_mode="paper")
    adapter = IBKRAdapter(settings, role="live_trading", read_only=False)
    adapter.ib = fake
    adapter.connect()
    return adapter


def test_confirmed_cancel_is_true():
    fake = FakeIB(final="Cancelled", delay_sleeps=3)
    fake.seed(16, 111)
    assert _adapter(fake).cancel_order("16") is True
    assert fake.cancelled == [16]


def test_api_cancelled_also_counts():
    fake = FakeIB(final="ApiCancelled")
    fake.seed(16, 111)
    assert _adapter(fake).cancel_order("16") is True


def test_a_fill_that_beats_the_cancel_is_false():
    fake = FakeIB(final="Filled", delay_sleeps=1)
    fake.seed(16, 111)
    assert _adapter(fake).cancel_order("16") is False


def test_no_final_state_within_the_timeout_is_false():
    fake = FakeIB(final=None)
    fake.seed(16, 111)
    assert _adapter(fake).cancel_order("16", timeout_s=0.01) is False


def test_an_order_not_in_this_session_is_false_and_untouched():
    fake = FakeIB()
    fake.seed(16, 111)
    assert _adapter(fake).cancel_order("99") is False
    assert fake.cancelled == []


def test_read_only_adapter_still_refuses():
    settings = IBKRSettings(_env_file=None, port=4002, trading_mode="paper")
    adapter = IBKRAdapter(settings, role="paper_sync", read_only=True)
    with pytest.raises(VenueReadOnlyError):
        adapter.cancel_order("16")
