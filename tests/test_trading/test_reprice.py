"""Repricing resting paper orders: cancel at the venue, book it, buy again at the new limit."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fees import FlatCommissionFeeModel
from src.domain.ids import MarketId
from src.domain.models import Market, Order, OrderAck, OrderRequest
from src.models.prediction import EdgeSignal
from src.trading.executor import TradeIntent, TradingExecutor
from src.trading.position_tracker import PositionTracker
from src.trading.reconcile import (
    drop_already_committed,
    load_open_commitments,
    reconcile_paper_book,
)
from src.trading.reprice import (
    plan_reprices,
    release_commitments,
    reprice_resting_orders,
    resting_book_rows,
)
from src.trading.risk_manager import RiskLimits
from tests.test_trading.test_executor_venue import FakeVenue

# The December 2026 book as placed on 2026-08-25, and the 2026-10-16 quotes.
BOOK = [  # strike, con_id, perm_id, venue order id, size, limit
    (1.2, 801, "5001", "16", 29.0, 0.34),
    (1.3, 802, "5002", "18", 41.0, 0.24),
    (1.4, 803, "5003", "20", 71.0, 0.14),
]
TODAY = {1.2: (0.959, 0.65), 1.3: (0.842, 0.44), 1.4: (0.607, 0.31)}  # p_model, ask


class BookVenue(FakeVenue):
    """FakeVenue with permIds, resting seeds, and a switch to make a cancel fail."""

    def __init__(self):
        super().__init__(read_only=False, fee=FlatCommissionFeeModel(0.01))
        self.refuse_cancel: set[str] = set()
        self.cancel_calls: list[str] = []
        self._perm = 9000

    def seed_resting(self, oid: str, perm: str, con_id: int, qty: float, price: float):
        self._orders[oid] = Order(
            order_id=oid, status="open", venue_meta={"perm_id": int(perm)},
            request=OrderRequest(market_id=MarketId.ibkr(con_id), side="BUY", leg="YES",
                                 quantity=qty, order_type="LMT", limit_price=price, tif="GTC"),
        )

    def get_all_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status == "open"]

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        if order_id in self.refuse_cancel:
            return False
        return super().cancel_order(order_id)

    def _place_order(self, request: OrderRequest) -> OrderAck:
        ack = super()._place_order(request)
        self._perm += 1
        if ack.order_id in self._orders:
            self._orders[ack.order_id] = self._orders[ack.order_id].model_copy(
                update={"venue_meta": {"perm_id": self._perm}})
        return ack.model_copy(update={"raw": {"perm_id": self._perm}})


def _market(con_id: int, strike: float) -> Market:
    return Market(
        market_id=MarketId.ibkr(con_id), no_id=MarketId.ibkr(con_id + 100), symbol="GTM",
        title=f"GTM Dec-2026 > {strike}", strike=strike, size_increment=1.0, tick_size=0.01,
        scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=60),
    )


def _intents() -> list[TradeIntent]:
    """As load_intents builds them at the ask, sorted by edge (1.3, 1.2, 1.4)."""
    out = []
    for strike, con_id, *_ in BOOK:
        p, ask = TODAY[strike]
        out.append(TradeIntent(
            edge=EdgeSignal(question_id=f"gtm:2026-12:{strike}", model_prob=p, market_prob=ask,
                            edge_pct=p - ask, expected_value=p - ask - 0.01,
                            kelly_fraction=0.3, confidence=1.0),
            market=_market(con_id, strike), title=f"GTM 2026-12 > {strike}",
        ))
    return sorted(out, key=lambda i: -i.edge.edge_pct)


def _book(tmp_path, venue: BookVenue) -> str:
    db = str(tmp_path / "ibkr_paper.db")
    PositionTracker(db_path=db)
    conn = sqlite3.connect(db)
    for strike, con_id, perm, oid, size, price in BOOK:
        conn.execute(
            """INSERT INTO trade_log (order_id, question_id, outcome, side, price, size,
               cost_usd, status, reason, created_at, filled_at, market_id, venue,
               venue_order_id, filled_size, perm_id, last_synced_utc)
               VALUES (?, ?, 'YES', 'BUY', ?, ?, ?, 'placed', 'edge', '2026-08-25T16:58:00',
                       NULL, ?, 'ibkr', ?, 0.0, ?, NULL)""",
            (f"b{oid}", f"gtm:2026-12:{strike}", price, size, round(price * size, 2),
             f"ibkr:{con_id}", oid, perm),
        )
        venue.seed_resting(oid, perm, con_id, size, price)
    conn.commit()
    conn.close()
    return db


def _executor(venue, db, max_group=15.0) -> TradingExecutor:
    return TradingExecutor(
        venue=venue, bankroll=100.0, db_path=db, simulate=False,
        risk_limits=RiskLimits(min_edge_pct=0.05, max_position_usd=10.0,
                               max_total_exposure_usd=50.0, max_group_usd=max_group),
    )


def _statuses(db) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT order_id, status FROM trade_log"))
    finally:
        conn.close()


def _run(venue, db, *, mode="auto", answers=None, max_group=15.0):
    """The paper entrypoint's lock block: reprice, reload, dedup, seed, place."""
    executor = _executor(venue, db, max_group)
    intents = _intents()
    commitments = load_open_commitments(db)
    resting = venue.get_all_open_orders()
    asked: list[str] = []

    def approve(prompt: str) -> str:
        asked.append(prompt)
        return answers.pop(0) if answers else "y"

    outcome = reprice_resting_orders(executor, venue, db, intents, resting, commitments,
                                     mode=mode, approval_input=approve)
    commitments = load_open_commitments(db)
    resting = [o for o in resting
               if str(o.venue_meta.get("perm_id")) not in outcome.cancelled_perm_ids]
    kept = drop_already_committed(intents, resting, commitments)
    executor.risk_manager.seed_exposure(commitments["total_usd"])
    executor.risk_manager.seed_group_exposure(commitments["by_group"])
    executor._approval_input = approve
    report = executor.execute_intents(kept, mode=mode, pre_approved=outcome.replacements)
    return outcome, report, asked


def test_plan_pairs_each_resting_order_with_a_higher_limit_on_its_leg(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    plan = plan_reprices(_intents(), resting_book_rows(db))
    assert {(c.question_id, c.new_price) for c in plan} == {
        ("gtm:2026-12:1.2", 0.65), ("gtm:2026-12:1.3", 0.44), ("gtm:2026-12:1.4", 0.31)}


def test_plan_leaves_an_order_already_at_or_above_the_limit(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    rows = resting_book_rows(db)
    for row in rows:
        row["price"] = 0.70
    assert plan_reprices(_intents(), rows) == []


def test_release_commitments_takes_the_rows_out():
    commitments = {"total_usd": 29.64, "by_market": {"ibkr:801": 9.86, "ibkr:802": 9.84},
                   "by_group": {"gtm:2026-12": 29.64}, "by_group_counts": {"gtm:2026-12": 3}}
    rows = [{"cost_usd": 9.86, "market_id": "ibkr:801", "question_id": "gtm:2026-12:1.2"}]
    out = release_commitments(commitments, rows)
    assert out["total_usd"] == pytest.approx(19.78)
    assert out["by_market"] == {"ibkr:802": 9.84}
    assert out["by_group"]["gtm:2026-12"] == pytest.approx(19.78)
    assert out["by_group_counts"]["gtm:2026-12"] == 2


def test_reprice_rebuilds_the_month_at_the_ask_within_the_group_cap(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)

    outcome, report, _ = _run(venue, db)

    assert set(venue.cancel_calls) == {"16", "18", "20"}
    assert set(_statuses(db).values()) >= {"cancelled"}
    assert all(_statuses(db)[f"b{oid}"] == "cancelled" for oid in ("16", "18", "20"))
    placed = {r.market_id.con_id: r for r in venue.requests}
    assert placed[802].limit_price == pytest.approx(0.44)  # highest edge first
    assert placed[801].limit_price == pytest.approx(0.65)
    assert 803 not in placed  # no room left under the $15 cap
    cost = sum(r.limit_price * r.quantity for r in venue.requests)
    assert cost <= 15.0 + 1e-9
    assert outcome.replacements == frozenset({"gtm:2026-12:1.3", "gtm:2026-12:1.2"})
    dropped = next(r for r in outcome.records if r["question_id"] == "gtm:2026-12:1.4")
    assert dropped["status"] == "cancelled" and dropped["replaced"] is False
    assert report.trades_executed == 2


def test_a_higher_group_cap_keeps_all_three_strikes(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    outcome, report, _ = _run(venue, db, max_group=30.0)
    assert {r.market_id.con_id for r in venue.requests} == {801, 802, 803}
    assert outcome.replacements == frozenset(
        {"gtm:2026-12:1.2", "gtm:2026-12:1.3", "gtm:2026-12:1.4"})


def test_approve_mode_asks_once_per_order_and_names_a_cancel_without_replacement(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    _, report, asked = _run(venue, db, mode="approve", answers=["y", "y", "y"])
    assert len(asked) == 3  # the replacements are not asked about a second time
    assert sum("NO replacement" in a for a in asked) == 1
    assert report.trades_executed == 2


def test_a_declined_order_stays_resting_and_keeps_its_room(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    # prompts arrive in book order: 1.2, 1.3, 1.4; decline 1.2
    outcome, _, _ = _run(venue, db, mode="approve", answers=["n", "y", "y", "y"])
    assert "16" not in venue.cancel_calls
    assert _statuses(db)["b16"] == "placed"
    assert "gtm:2026-12:1.2" not in outcome.replacements
    assert all(r.market_id.con_id != 801 for r in venue.requests)


def test_an_unconfirmed_cancel_keeps_the_book_row_and_places_nothing_on_that_leg(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    venue.refuse_cancel.add("18")
    outcome, _, _ = _run(venue, db)
    assert _statuses(db)["b18"] == "placed"
    assert "5002" not in outcome.cancelled_perm_ids
    assert all(r.market_id.con_id != 802 for r in venue.requests)
    assert any("not confirmed" in n for n in outcome.notes)


def test_an_order_from_another_client_is_left_alone(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    venue.get_open_orders = lambda: [o for o in venue._orders.values()
                                     if o.order_id != "20" and o.status == "open"]
    outcome, _, _ = _run(venue, db)
    assert "20" not in venue.cancel_calls
    assert _statuses(db)["b20"] == "placed"
    assert any("not among this session's open orders" in n for n in outcome.notes)


def test_the_next_sync_does_not_read_the_reprice_as_a_mass_cancel(tmp_path):
    venue = BookVenue()
    db = _book(tmp_path, venue)
    _run(venue, db)
    report = reconcile_paper_book(db, venue)
    assert report.aborted is False
    assert report.cancelled == 0
    assert report.examined == 2 and report.unchanged == 2
