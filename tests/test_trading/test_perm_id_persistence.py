"""Task 1.3: the venue's permId (assigned at order ack) must survive into
both durable records -- trade_log (SQLite) and the ibkr_paper_runs.jsonl
run ledger -- so a later reconciliation task can join the paper book to
venue state by permId (str-normalized on both sides).
"""

from __future__ import annotations

import sqlite3

from src.domain.models import Order, OrderAck
from src.trading.executor import TradeIntent, TradingExecutor
from tests.test_trading.test_executor_venue import FakeVenue, _edge, _ibkr_market


class FakeVenueWithPermId(FakeVenue):
    """FakeVenue whose ack carries a venue permId, like the real IBKR adapter
    (src/venues/ibkr/adapter.py: `raw={"perm_id": getattr(trade.order, "permId", None), ...}`).
    """

    def _place_order(self, request):
        self.requests.append(request)
        self._n += 1
        oid = f"V{self._n}"
        self._orders[oid] = Order(order_id=oid, request=request, status="open")
        return OrderAck(order_id=oid, accepted=True, detail="Submitted",
                        raw={"perm_id": 555000111})


def test_perm_id_flows_ack_to_db(tmp_path):
    venue = FakeVenueWithPermId(read_only=False)
    ex = TradingExecutor(venue=venue, bankroll=100.0, db_path=str(tmp_path / "p.db"), simulate=False)
    rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    order = rep.decisions[0].order
    assert order.perm_id == "555000111"
    row = sqlite3.connect(tmp_path / "p.db").execute(
        "SELECT perm_id, status FROM trade_log WHERE order_id = ?", (order.order_id,)).fetchone()
    assert row == ("555000111", "placed")
