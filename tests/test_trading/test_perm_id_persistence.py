"""The venue's permId must survive into both trade_log and the run ledger."""

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


# I-1: permId not yet assigned at ack time


class FakeVenueLatePermId(FakeVenue):
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


def test_a_failing_backfill_read_does_not_crash_sync(tmp_path, caplog):
    import logging

    class _FlakyReadVenue(FakeVenueLatePermId):
        def get_all_open_orders(self):
            self.all_open_calls += 1
            raise RuntimeError("gateway hiccup")

    venue = _FlakyReadVenue(ack_perm_id=None)
    ex, order, db = _place_one(tmp_path, venue)
    with caplog.at_level(logging.ERROR, logger="src.trading.order_manager"):
        ex.sync()  # must not raise
    assert venue.all_open_calls == 1
    assert order.perm_id == ""
    assert _db_perm_id(db, order.order_id) == ""
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


def test_simulated_executor_sync_makes_no_backfill_venue_call(tmp_path):
    venue = FakeVenueLatePermId(ack_perm_id=None)
    ex = TradingExecutor(venue=venue, bankroll=100.0, db_path=str(tmp_path / "s.db"),
                         simulate=True)
    ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    assert ex.sync() == 0
    assert venue.all_open_calls == 0


class _FakeSettings:
    trading_mode = "paper"

    def __init__(self, root: Path):
        self.resolved_data_root = root


class _FakeSyncAdapter:
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
    rc, record = _run_sync_main(tmp_path, monkeypatch, perm_id_on_file="1599223399")
    assert record["skipped_missing_perm_id"] == 0
    assert record["unchanged"] == 1
    assert record["ok"] is True
    assert rc == 0
