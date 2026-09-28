"""Task 1.3: the venue's permId (assigned at order ack) must survive into
both durable records -- trade_log (SQLite) and the ibkr_paper_runs.jsonl
run ledger -- so a later reconciliation task can join the paper book to
venue state by permId (str-normalized on both sides).

Final-review finding I-1 (below the original test): the adapter reads
``trade.order.permId`` after a fixed 1s sleep, so a slow Gateway can ack an
order before IB has assigned its permId. That row used to be stored with
``perm_id = ""`` forever -- ``reconcile_paper_book`` skips it on every run
with only a note while the sync ledger kept ``ok: true``, silently
dropping the order out of fill capture. Two fixes are pinned here:

- BACKFILL: ``TradingExecutor.sync()`` re-reads the venue's open orders
  (read-only) and fills in any of this run's missing permIds, matched by
  venue order id AND leg contract.
- LOUDNESS: a candidate row still missing its permId is counted
  (``ReconcileReport.skipped_missing_perm_id``) and makes the sync run
  record ``ok: false`` / exit 1, so the health probe's SYNC_STALE axis
  fires instead of the gap staying silent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.domain.ids import MarketId
from src.domain.models import Order, OrderAck, OrderRequest
from src.trading.executor import TradeIntent, TradingExecutor
from src.trading.reconcile import reconcile_paper_book
from tests.test_trading.test_executor_venue import FakeVenue, _edge, _ibkr_market

YES_CON = 712856754  # _ibkr_market()'s YES leg


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


# --------------------------------------------------------------------------- #
# I-1: permId not yet assigned at ack time
# --------------------------------------------------------------------------- #


class FakeVenueLatePermId(FakeVenue):
    """The ack arrives before IB assigns a permId (``raw["perm_id"]`` is
    ``None`` or ``0``, both seen from ib_async); the venue's open-order list
    (``get_all_open_orders``, like ``IBKRAdapter``'s reqAllOpenOrders read)
    carries it once assigned -- ``assigned`` maps venue order id -> permId,
    and an unassigned one reads back as 0 exactly like ``_trade_to_order``.
    ``foreign`` orders are other clients' resting orders.
    """

    def __init__(self, ack_perm_id=None, **kwargs):
        super().__init__(read_only=False, **kwargs)
        self._ack_perm_id = ack_perm_id
        self.assigned: dict[str, int] = {}
        self.foreign: list[Order] = []
        self.all_open_calls = 0
        self.cancels: list[str] = []

    def _place_order(self, request):
        self.requests.append(request)
        self._n += 1
        oid = f"V{self._n}"
        self._orders[oid] = Order(order_id=oid, request=request, status="open")
        return OrderAck(order_id=oid, accepted=True, detail="Submitted",
                        raw={"perm_id": self._ack_perm_id})

    def get_all_open_orders(self) -> list[Order]:
        self.all_open_calls += 1
        own = [
            o.model_copy(update={"venue_meta": {"perm_id": self.assigned.get(oid, 0)}})
            for oid, o in self._orders.items() if o.status in ("pending", "open")
        ]
        return own + list(self.foreign)

    def cancel_order(self, order_id: str) -> bool:  # pragma: no cover - must never run
        self.cancels.append(order_id)
        return super().cancel_order(order_id)


def _db_perm_id(db: Path, order_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT perm_id FROM trade_log WHERE order_id = ?", (order_id,)).fetchone()[0]
    finally:
        conn.close()


def _place_one(tmp_path, venue) -> tuple[TradingExecutor, object, Path]:
    db = tmp_path / "p.db"
    ex = TradingExecutor(venue=venue, bankroll=100.0, db_path=str(db), simulate=False)
    rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    order = rep.decisions[0].order
    assert order is not None and order.venue_order_id == "V1"
    return ex, order, db


@pytest.mark.parametrize("ack_perm_id", [None, 0])
def test_missing_perm_id_at_ack_is_stored_empty(tmp_path, ack_perm_id):
    """Pins the CURRENT storage: None and 0 both become "" -- never "None"
    or "0", which reconcile would treat as a real permId and join on."""
    venue = FakeVenueLatePermId(ack_perm_id=ack_perm_id)
    _, order, db = _place_one(tmp_path, venue)
    assert order.perm_id == ""
    assert _db_perm_id(db, order.order_id) == ""


@pytest.mark.parametrize("ack_perm_id", [None, 0])
def test_sync_backfills_a_missing_perm_id_from_the_venue_open_orders(tmp_path, ack_perm_id):
    venue = FakeVenueLatePermId(ack_perm_id=ack_perm_id)
    ex, order, db = _place_one(tmp_path, venue)
    assert _db_perm_id(db, order.order_id) == ""

    venue.assigned["V1"] = 1599223399  # IB has assigned it by sync time
    ex.sync()

    assert order.perm_id == "1599223399"
    assert _db_perm_id(db, order.order_id) == "1599223399"
    # Read-only: the backfill placed and cancelled nothing.
    assert len(venue.requests) == 1
    assert venue.cancels == []
    assert venue.all_open_calls == 1


def test_backfilled_row_then_reconciles_instead_of_being_skipped(tmp_path):
    """End to end: once backfilled, the row joins the venue's open order by
    permId and is concluded, not skipped."""
    venue = FakeVenueLatePermId(ack_perm_id=None)
    ex, order, db = _place_one(tmp_path, venue)
    venue.assigned["V1"] = 1599223399
    ex.sync()

    report = reconcile_paper_book(str(db), venue)
    assert report.skipped_missing_perm_id == 0
    assert report.unchanged == 1


def test_backfill_leaves_the_row_empty_when_the_venue_still_has_no_perm_id(tmp_path):
    venue = FakeVenueLatePermId(ack_perm_id=None)  # never assigned: reads back 0
    ex, order, db = _place_one(tmp_path, venue)
    ex.sync()
    assert order.perm_id == ""
    assert _db_perm_id(db, order.order_id) == ""

    # ...and the next reconcile run says so loudly instead of silently.
    report = reconcile_paper_book(str(db), venue)
    assert report.skipped_missing_perm_id == 1


def test_backfill_never_matches_on_order_id_alone(tmp_path):
    """IBKR order ids are per-client: another client's resting order can
    share our venue order id. Only an order on the SAME leg contract may
    donate its permId."""
    venue = FakeVenueLatePermId(ack_perm_id=None)
    venue.foreign.append(Order(
        order_id="V1",  # same id, different client and contract
        request=OrderRequest(market_id=MarketId.ibkr(999999), side="BUY", leg="YES",
                             quantity=3, order_type="LMT", limit_price=0.2, tif="GTC"),
        status="open", venue_meta={"perm_id": 424242},
    ))
    ex, order, db = _place_one(tmp_path, venue)
    ex.sync()
    assert order.perm_id == ""
    assert _db_perm_id(db, order.order_id) == ""


def test_backfill_refuses_an_ambiguous_match(tmp_path):
    """Two different permIds claiming our (order id, contract) -- guessing
    between them could attach our row to someone else's order. Leave it
    empty (reconcile then counts it loudly)."""
    venue = FakeVenueLatePermId(ack_perm_id=None)
    venue.assigned["V1"] = 1599223399
    venue.foreign.append(Order(
        order_id="V1",
        request=OrderRequest(market_id=MarketId.ibkr(YES_CON), side="BUY", leg="YES",
                             quantity=3, order_type="LMT", limit_price=0.2, tif="GTC"),
        status="open", venue_meta={"perm_id": 424242},
    ))
    ex, order, db = _place_one(tmp_path, venue)
    ex.sync()
    assert order.perm_id == ""
    assert _db_perm_id(db, order.order_id) == ""


def test_backfill_never_overwrites_a_perm_id_already_on_file(tmp_path):
    venue = FakeVenueLatePermId(ack_perm_id=555000111)
    venue.assigned["V1"] = 777  # a (hypothetically) different read-back
    ex, order, db = _place_one(tmp_path, venue)
    ex.sync()
    assert order.perm_id == "555000111"
    assert _db_perm_id(db, order.order_id) == "555000111"
    assert venue.all_open_calls == 0  # nothing missing -> no venue read at all


def test_simulated_executor_sync_makes_no_backfill_venue_call(tmp_path):
    venue = FakeVenueLatePermId(ack_perm_id=None)
    ex = TradingExecutor(venue=venue, bankroll=100.0, db_path=str(tmp_path / "s.db"),
                         simulate=True)
    ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    assert ex.sync() == 0
    assert venue.all_open_calls == 0


# --------------------------------------------------------------------------- #
# I-1 loudness: scripts/sync_ibkr_paper.py main() end to end (fake adapter,
# fake settings -- no Gateway, no .env, tmp_path book only)
# --------------------------------------------------------------------------- #


class _FakeSettings:
    trading_mode = "paper"

    def __init__(self, root: Path):
        self.resolved_data_root = root


class _FakeSyncAdapter:
    """Read-only stand-in for IBKRAdapter(role="paper_sync"): only what
    sync_ibkr_paper.main() and reconcile_paper_book touch."""

    open_orders: list[Order] = []

    def __init__(self, settings, role, read_only):
        assert read_only is True and role == "paper_sync"
        self.role, self.read_only = role, read_only
        self.mode, self.account = "paper", "DU0000000"

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    def get_all_open_orders(self) -> list[Order]:
        return list(self.open_orders)

    def get_positions(self) -> list:
        return []


def _book_with_one_placed_row(db: Path, perm_id: str) -> None:
    from src.trading.position_tracker import PositionTracker

    PositionTracker(db_path=str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd, status,
            reason, created_at, filled_at, market_id, venue, venue_order_id,
            filled_size, perm_id, last_synced_utc)
           VALUES ('a1', 'gtm:2026-12:1.2', 'YES', 'BUY', 0.34, 29, 9.86, 'placed',
                   'edge', '2026-09-28T10:00:00', NULL, ?, 'ibkr', 'V1', 0, ?, NULL)""",
        (f"ibkr:{YES_CON}", perm_id),
    )
    conn.commit()
    conn.close()


def _resting(perm_id: int) -> Order:
    return Order(
        order_id="V1",
        request=OrderRequest(market_id=MarketId.ibkr(YES_CON), side="BUY", leg="YES",
                             quantity=29, order_type="LMT", limit_price=0.34, tif="GTC"),
        status="open", venue_meta={"perm_id": perm_id},
    )


def _run_sync_main(tmp_path, monkeypatch, perm_id_on_file: str) -> tuple[int, dict]:
    import scripts.sync_ibkr_paper as sync_mod

    db = tmp_path / "paper" / "ibkr_paper.db"
    db.parent.mkdir(parents=True)
    _book_with_one_placed_row(db, perm_id_on_file)
    monkeypatch.setattr(sync_mod, "IBKRSettings", lambda: _FakeSettings(tmp_path))
    monkeypatch.setattr(_FakeSyncAdapter, "open_orders", [_resting(1599223399)])
    monkeypatch.setattr(sync_mod, "IBKRAdapter", _FakeSyncAdapter)

    rc = sync_mod.main(["--db", str(db)])
    lines = (db.parent / sync_mod.RUNS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return rc, json.loads(lines[0])


def test_sync_main_marks_a_missing_perm_id_run_unhealthy(tmp_path, monkeypatch):
    rc, record = _run_sync_main(tmp_path, monkeypatch, perm_id_on_file="")
    assert record["skipped_missing_perm_id"] == 1
    assert record["aborted"] is False  # not a guard abort -- a distinct failure
    assert record["ok"] is False
    assert rc == 1


def test_sync_main_stays_healthy_when_every_row_has_a_perm_id(tmp_path, monkeypatch):
    """Contrast case, so the unhealthy verdict above is provably caused by
    the missing permId and nothing else in the fixture."""
    rc, record = _run_sync_main(tmp_path, monkeypatch, perm_id_on_file="1599223399")
    assert record["skipped_missing_perm_id"] == 0
    assert record["unchanged"] == 1
    assert record["ok"] is True
    assert rc == 0
