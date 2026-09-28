"""Task 1.5: durable paper-book reconciliation (`src/trading/reconcile.py`).

A GTC paper order rests at the venue for days or weeks. Nothing in the
system used to record what happened to it: order state lived in-process
and died with the run. These tests pin the durable replacement -- venue
truth (open orders + positions) written into `ibkr_paper.db` so a 365-day
paper measurement can state its own fills.

Everything here runs against tmp SQLite files and a list-backed
`FakeSyncVenue`; no Gateway, no network, no real book.
"""

from __future__ import annotations

import gc
import sqlite3

import pytest

from src.domain.ids import MarketId
from src.domain.models import Order, OrderRequest, Position
from src.trading.position_tracker import PositionTracker
from src.trading.reconcile import ReconcileReport, reconcile_paper_book

YES_CON = 712856754
NO_CON = 712856759
OTHER_CON = 712856799


# ------------------------------------------------------------------ fakes
class FakeSyncVenue:
    """Duck-typed stand-in for the IBKR adapter: only the two fetches
    reconciliation is allowed to use, backed by plain lists.
    """

    def __init__(self, open_orders: list[Order] | None = None,
                 positions: list[Position] | None = None):
        self.open_orders = list(open_orders or [])
        self.positions = list(positions or [])
        self.calls: list[str] = []

    def get_all_open_orders(self) -> list[Order]:
        self.calls.append("get_all_open_orders")
        return list(self.open_orders)

    def get_positions(self) -> list[Position]:
        self.calls.append("get_positions")
        return list(self.positions)


class BoomVenue(FakeSyncVenue):
    """Open orders answer normally; the positions fetch blows up. Used to
    fail a run *after* the first row has already been written.
    """

    def get_positions(self) -> list[Position]:
        self.calls.append("get_positions")
        raise RuntimeError("gateway dropped mid-sync")


def _venue_order(perm_id, con_id: int = YES_CON, quantity: float = 29.0,
                 filled: float = 0.0, avg_price: float | None = None,
                 status: str = "open", order_id: str = "V1",
                 leg: str = "YES") -> Order:
    return Order(
        order_id=order_id,
        request=OrderRequest(
            market_id=MarketId.ibkr(con_id), side="BUY", leg=leg,
            quantity=quantity, order_type="LMT", limit_price=0.34, tif="GTC",
        ),
        status=status,
        filled_quantity=filled,
        avg_fill_price=avg_price,
        venue_meta={"perm_id": perm_id},
    )


def _venue_position(con_id: int = YES_CON, quantity: float = 29.0,
                    avg_price: float = 0.34, leg: str = "YES") -> Position:
    return Position(market_id=MarketId.ibkr(con_id), leg=leg,
                    quantity=quantity, avg_price=avg_price)


# ------------------------------------------------------------------ db helpers
def _make_book(path) -> None:
    """Create a v2-shaped paper book (PositionTracker owns the DDL)."""
    PositionTracker(db_path=str(path))


def _insert_order_row(path, order_id: str = "a1", perm_id: str = "111",
                      con_id: int = YES_CON, size: float = 29.0, price: float = 0.34,
                      status: str = "placed", filled_size: float = 0.0,
                      outcome: str = "YES", question_id: str = "gtm-2026-12-1.2") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd, status,
            reason, created_at, filled_at, market_id, venue, venue_order_id,
            filled_size, perm_id, last_synced_utc)
           VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, 'edge', '2026-08-25T10:00:00', NULL,
                   ?, 'ibkr', 'V1', ?, ?, NULL)""",
        (order_id, question_id, outcome, price, size, round(price * size, 2), status,
         f"ibkr:{con_id}", filled_size, perm_id),
    )
    conn.commit()
    conn.close()


def _add_resting_companion(db, venue: FakeSyncVenue) -> None:
    """Give the book a second row that is still resting at the venue.

    R8's mass-cancel guard aborts any run that would cancel a row while the
    venue's open-order list is EMPTY -- a dropped Gateway session looks
    exactly like that. A legitimate single cancellation therefore needs the
    venue to still be showing something, which is also what reality looks
    like: one order gone, others still working.
    """
    _insert_order_row(db, order_id="z9", perm_id="999", con_id=OTHER_CON, size=5.0,
                      question_id="gtm-2026-12-9.9")
    venue.open_orders.append(
        _venue_order(perm_id=999, con_id=OTHER_CON, quantity=5.0, order_id="V9"))


def _insert_position_row(path, order_id: str, size: float, entry_price: float = 0.34,
                         con_id: int = YES_CON, question_id: str = "gtm-2026-12-1.2",
                         outcome: str = "YES",
                         opened_at: str = "2026-08-20T10:00:00+00:00") -> None:
    """A position an EARLIER run already booked (still open at the venue)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO positions
           (question_id, outcome, entry_price, size, cost_basis, opened_at,
            market_id, venue, order_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'ibkr', ?)""",
        (question_id, outcome, entry_price, size, round(entry_price * size, 2),
         opened_at, f"ibkr:{con_id}", order_id),
    )
    conn.commit()
    conn.close()


def _insert_fill_event(path, perm_id: str, order_id: str, cum: float, delta: float,
                       avg_price: float | None = 0.34,
                       source: str = "sync_open_order") -> None:
    from src.trading.paper_schema import FILL_EVENTS_DDL

    conn = sqlite3.connect(path)
    conn.execute(FILL_EVENTS_DDL)
    conn.execute(
        """INSERT INTO fill_events
           (perm_id, order_id, ts_utc, detected_ts_utc, cum_filled, fill_delta,
            avg_price, commission_usd, source)
           VALUES (?, ?, '2026-08-25T12:00:00+00:00', '2026-08-25T12:00:00+00:00',
                   ?, ?, ?, NULL, ?)""",
        (perm_id, order_id, cum, delta, avg_price, source),
    )
    conn.commit()
    conn.close()


def _trade_row(path, order_id: str = "a1") -> sqlite3.Row:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trade_log WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def _fill_events(path) -> list[sqlite3.Row]:
    """Fill events on disk. A book whose run rolled back has no such table
    (reconcile creates it inside the same transaction) -- that is "no events".
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fill_events'"
        ).fetchone():
            return []
        return conn.execute("SELECT * FROM fill_events ORDER BY cum_filled").fetchall()
    finally:
        conn.close()


def _positions(path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM positions ORDER BY order_id").fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------ (a) resting
def test_resting_order_unchanged_touches_only_last_synced(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=111)])

    report = reconcile_paper_book(str(db), venue)

    assert isinstance(report, ReconcileReport)
    assert (report.examined, report.unchanged) == (1, 1)
    assert report.filled == report.partially_filled == report.cancelled == 0
    assert report.unmatched_venue_orders == 0
    assert report.notes == []

    row = _trade_row(db)
    assert row["status"] == "placed"
    assert row["filled_size"] == 0.0
    assert row["last_synced_utc"]  # stamped even when nothing changed
    assert _fill_events(db) == []
    assert _positions(db) == []
    # Positions are only fetched when a row actually needs them.
    assert "get_positions" not in venue.calls


def test_perm_id_join_normalizes_int_venue_side_against_text_db_side(tmp_path):
    """R1: the venue hands back an INT permId; the DB column is TEXT."""
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, perm_id="555000111")
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=555000111, filled=4.0,
                                                    avg_price=0.34)])

    report = reconcile_paper_book(str(db), venue)

    assert report.partially_filled == 1 and report.unmatched_venue_orders == 0


# ------------------------------------------------------------------ (b) partial
def test_partial_fill_on_a_resting_order_is_recorded(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=111, filled=10.0,
                                                    avg_price=0.34)])

    report = reconcile_paper_book(str(db), venue)

    assert (report.examined, report.partially_filled, report.unchanged) == (1, 1, 0)
    row = _trade_row(db)
    assert row["status"] == "partially_filled"
    assert row["filled_size"] == pytest.approx(10.0)

    events = _fill_events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["perm_id"] == "111" and ev["order_id"] == "a1"
    assert ev["cum_filled"] == pytest.approx(10.0)
    assert ev["fill_delta"] == pytest.approx(10.0)
    assert ev["avg_price"] == pytest.approx(0.34)
    assert ev["source"] == "sync_open_order"
    assert ev["commission_usd"] is None
    # Detection time is not fill time, but both columns carry it today.
    assert ev["ts_utc"] == ev["detected_ts_utc"] == row["last_synced_utc"]
    # A partial fill is not a position yet -- the order is still working.
    assert _positions(db) == []


def test_second_partial_appends_only_the_new_delta(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)])
    reconcile_paper_book(str(db), venue)

    venue.open_orders = [_venue_order(perm_id=111, filled=15.0, avg_price=0.35)]
    report = reconcile_paper_book(str(db), venue)

    assert report.partially_filled == 1
    events = _fill_events(db)
    assert [(e["cum_filled"], e["fill_delta"]) for e in events] == [(10.0, 10.0), (15.0, 5.0)]
    assert _trade_row(db)["filled_size"] == pytest.approx(15.0)


# ------------------------------------------------------------------ (c) filled
def test_order_gone_with_covering_position_is_filled_and_opens_a_position(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0,
                                                                    avg_price=0.34)])

    report = reconcile_paper_book(str(db), venue)

    assert (report.examined, report.filled, report.cancelled) == (1, 1, 0)
    row = _trade_row(db)
    assert row["status"] == "filled"
    assert row["filled_size"] == pytest.approx(29.0)

    events = _fill_events(db)
    assert len(events) == 1
    assert events[0]["cum_filled"] == pytest.approx(29.0)
    assert events[0]["fill_delta"] == pytest.approx(29.0)
    assert events[0]["source"] == "sync_position_diff"
    assert events[0]["avg_price"] == pytest.approx(0.34)

    positions = _positions(db)
    assert len(positions) == 1
    pos = positions[0]
    assert pos["order_id"] == "a1" and pos["question_id"] == "gtm-2026-12-1.2"
    assert pos["outcome"] == "YES"
    assert pos["entry_price"] == pytest.approx(0.34)
    assert pos["size"] == pytest.approx(29.0)
    assert pos["cost_basis"] == pytest.approx(round(0.34 * 29, 2))
    assert pos["market_id"] == f"ibkr:{YES_CON}" and pos["venue"] == "ibkr"
    assert pos["closed_at"] is None


def test_fill_after_a_recorded_partial_only_books_the_remainder(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, status="partially_filled", filled_size=10.0)
    _insert_fill_event(db, "111", "a1", cum=10.0, delta=10.0)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 1
    events = _fill_events(db)
    assert [(e["cum_filled"], e["fill_delta"]) for e in events] == [(10.0, 10.0), (29.0, 19.0)]
    assert _positions(db)[0]["size"] == pytest.approx(29.0)


def test_no_leg_row_joins_on_its_own_con_id(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a3", perm_id="333", con_id=NO_CON, size=71.0,
                      price=0.14, outcome="NO")
    venue = FakeSyncVenue(
        open_orders=[],
        # A YES-leg position on the *other* contract must not satisfy the NO row.
        positions=[_venue_position(con_id=YES_CON, quantity=500.0),
                   _venue_position(con_id=NO_CON, quantity=71.0, avg_price=0.14, leg="NO")],
    )

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 1
    pos = _positions(db)[0]
    assert pos["outcome"] == "NO" and pos["market_id"] == f"ibkr:{NO_CON}"
    assert pos["size"] == pytest.approx(71.0)


# ------------------------------------------------------------------ (d) cancelled
def test_order_gone_with_no_position_is_cancelled(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[], positions=[])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert (report.examined, report.cancelled, report.filled) == (2, 1, 0)
    assert report.aborted is False
    row = _trade_row(db)
    assert row["status"] == "cancelled"
    assert row["filled_size"] == 0.0
    assert row["last_synced_utc"]
    assert _fill_events(db) == []
    assert _positions(db) == []  # nothing filled -> nothing to hold


def test_smaller_position_than_ordered_is_cancelled_and_noted(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)  # size 29
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=5.0)])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert report.cancelled == 1 and report.filled == 0
    assert _trade_row(db)["status"] == "cancelled"
    # 5 contracts sit at the venue but no fill was ever recorded: surfaced, never invented.
    assert any("a1" in n and "5" in n for n in report.notes)
    assert _fill_events(db) == []
    assert _positions(db) == []


# ------------------------------------------------------------------ (e) partial then cancelled (M1)
def test_partial_then_cancelled_keeps_the_partial_position(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, status="partially_filled", filled_size=10.0)
    _insert_fill_event(db, "111", "a1", cum=10.0, delta=10.0)
    venue = FakeSyncVenue(open_orders=[],
                          positions=[_venue_position(quantity=10.0, avg_price=0.33)])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert (report.cancelled, report.filled) == (1, 0)
    row = _trade_row(db)
    assert row["status"] == "cancelled"
    assert row["filled_size"] == pytest.approx(10.0)  # the partial is kept, not erased

    positions = _positions(db)
    assert len(positions) == 1
    assert positions[0]["size"] == pytest.approx(10.0)
    assert positions[0]["entry_price"] == pytest.approx(0.33)
    assert positions[0]["cost_basis"] == pytest.approx(round(0.33 * 10, 2))
    # No new fill_event: nothing new filled, the order was simply cancelled.
    assert len(_fill_events(db)) == 1


def test_partial_then_cancelled_notes_a_position_quantity_mismatch(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, status="partially_filled", filled_size=10.0)
    _insert_fill_event(db, "111", "a1", cum=10.0, delta=10.0)
    venue = FakeSyncVenue(open_orders=[],
                          positions=[_venue_position(quantity=7.0, avg_price=0.33)])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert report.cancelled == 1
    assert any("a1" in n for n in report.notes)
    # The recorded fills stay authoritative -- the mismatch is reported, not guessed away.
    assert _positions(db)[0]["size"] == pytest.approx(10.0)


# ------------------------------------------------------------------ position attribution (C-1)
def test_a_standing_position_from_an_earlier_run_never_books_a_later_order(tmp_path):
    """The mainline hold-to-resolution failure: a1 filled weeks ago and the
    venue still holds its 29 contracts. A later order a2 on the SAME contract
    is cancelled at the venue -- the standing position must not be re-used to
    fabricate a fill for it.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_position_row(db, order_id="a1", size=29.0)   # booked by an earlier run
    _insert_order_row(db, order_id="a2", perm_id="222", size=29.0)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert (report.filled, report.cancelled) == (0, 1)
    assert _trade_row(db, "a2")["status"] == "cancelled"
    assert _trade_row(db, "a2")["filled_size"] == 0.0
    assert _fill_events(db) == []                        # no fill invented
    assert [p["order_id"] for p in _positions(db)] == ["a1"]  # no second position
    assert any("a2" in n and "unattributed" in n for n in report.notes)


def test_one_position_covering_only_one_of_two_orders_fills_only_one(tmp_path):
    """In-run variant: two candidates on one contract, a position big enough
    for exactly one of them.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111", size=29.0)
    _insert_order_row(db, order_id="a2", perm_id="222", size=29.0)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])
    _add_resting_companion(db, venue)

    report = reconcile_paper_book(str(db), venue)

    assert (report.filled, report.cancelled) == (1, 1)
    assert _trade_row(db, "a1")["status"] == "filled"
    assert _trade_row(db, "a2")["status"] == "cancelled"
    assert [p["order_id"] for p in _positions(db)] == ["a1"]
    assert len(_fill_events(db)) == 1
    assert any("a2" in n and "unattributed" in n for n in report.notes)


def test_a_closed_position_does_not_reserve_venue_quantity(tmp_path):
    """Only OPEN positions rows hold quantity: a closed one is gone from the
    venue too, so it must not block a later fill.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_position_row(db, order_id="a0", size=29.0)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE positions SET closed_at = '2026-08-24T00:00:00+00:00', "
                 "exit_price = 1.0, realized_pnl = 19.14 WHERE order_id = 'a0'")
    conn.commit()
    conn.close()
    _insert_order_row(db, order_id="a2", perm_id="222", size=29.0)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 1
    assert _trade_row(db, "a2")["status"] == "filled"


# ------------------------------------------------------------------ blended avg price (I-2)
def test_position_larger_than_the_order_flags_the_blended_average(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, size=29.0)
    # 40 contracts at a blended 0.34: 29 are this order's, the rest are not.
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=40.0,
                                                                    avg_price=0.34)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 1
    blended = [n for n in report.notes if "blended" in n.lower()]
    assert len(blended) == 1
    note = blended[0]
    assert "a1" in note and str(YES_CON) in note and "40" in note and "29" in note
    assert "0.34" in note
    # The number written is still the venue's, unchanged -- the fix is the note.
    assert _positions(db)[0]["entry_price"] == pytest.approx(0.34)


def test_a_position_that_matches_the_order_exactly_says_nothing_about_blending(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, size=29.0)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 1
    assert not [n for n in report.notes if "blended" in n.lower()]


# ------------------------------------------------------------------ mass-cancel guard (I-1 / R8)
def test_an_empty_open_order_list_aborts_instead_of_cancelling_the_book(tmp_path):
    """A dropped Gateway session answers with an empty list, which is
    indistinguishable from "everything was cancelled". Abort, write nothing.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[], positions=[])

    report = reconcile_paper_book(str(db), venue)

    assert report.aborted is True
    assert report.examined == 1
    assert report.cancelled == report.filled == report.partially_filled == 0
    assert any("ABORTED" in n for n in report.notes)

    row = _trade_row(db)
    assert row["status"] == "placed"
    assert row["last_synced_utc"] is None   # not even a timestamp was written
    assert _fill_events(db) == []
    assert _positions(db) == []
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "fill_events" not in tables      # the whole transaction rolled back


def test_every_order_filled_on_an_empty_open_list_still_concludes(tmp_path):
    """The end state of a hold-to-resolution book: nothing resting, everything
    filled. The guard must not deadlock it.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111", con_id=YES_CON, size=29.0)
    _insert_order_row(db, order_id="a2", perm_id="222", con_id=NO_CON, size=71.0,
                      price=0.14, outcome="NO", question_id="gtm-2026-12-1.5")
    venue = FakeSyncVenue(
        open_orders=[],
        positions=[_venue_position(con_id=YES_CON, quantity=29.0),
                   _venue_position(con_id=NO_CON, quantity=71.0, avg_price=0.14, leg="NO")],
    )

    report = reconcile_paper_book(str(db), venue)

    assert report.aborted is False
    assert report.filled == 2 and report.cancelled == 0
    assert _trade_row(db, "a1")["status"] == "filled"
    assert _trade_row(db, "a2")["status"] == "filled"


def test_cancelling_more_than_half_the_book_aborts_even_with_a_live_open_list(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111")
    _insert_order_row(db, order_id="a2", perm_id="222", question_id="gtm-2026-12-1.5")
    venue = FakeSyncVenue(open_orders=[], positions=[])
    _add_resting_companion(db, venue)  # 3 candidates, 2 would be cancelled -> 67%

    report = reconcile_paper_book(str(db), venue)

    assert report.aborted is True
    assert report.cancelled == 0
    assert any("ABORTED" in n and "2 of 3" in n for n in report.notes)
    for order_id in ("a1", "a2", "z9"):
        row = _trade_row(db, order_id)
        assert row["status"] == "placed"
        assert row["last_synced_utc"] is None


def test_half_the_book_cancelled_is_below_the_guard(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111")
    venue = FakeSyncVenue(open_orders=[], positions=[])
    _add_resting_companion(db, venue)  # 2 candidates, 1 cancelled -> exactly 50%

    report = reconcile_paper_book(str(db), venue)

    assert report.aborted is False
    assert report.cancelled == 1 and report.unchanged == 1


def test_allow_mass_cancel_proceeds_through_both_guards(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111")
    _insert_order_row(db, order_id="a2", perm_id="222", question_id="gtm-2026-12-1.5")
    venue = FakeSyncVenue(open_orders=[], positions=[])

    report = reconcile_paper_book(str(db), venue, allow_mass_cancel=True)

    assert report.aborted is False
    assert report.cancelled == 2
    assert _trade_row(db, "a1")["status"] == "cancelled"
    assert _trade_row(db, "a2")["status"] == "cancelled"


def test_an_aborted_run_can_be_rerun_once_the_venue_answers_properly(tmp_path):
    """The guard costs nothing but a retry: the book is untouched, so the next
    healthy sync reconciles it normally.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)

    assert reconcile_paper_book(str(db), FakeSyncVenue(open_orders=[])).aborted is True

    healthy = FakeSyncVenue(open_orders=[_venue_order(perm_id=111, filled=10.0,
                                                      avg_price=0.34)])
    report = reconcile_paper_book(str(db), healthy)

    assert report.aborted is False and report.partially_filled == 1
    assert _trade_row(db)["filled_size"] == pytest.approx(10.0)


# ------------------------------------------------------------------ perm_id / foreign orders
def test_row_without_a_perm_id_is_skipped_and_noted(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, perm_id="")
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == report.cancelled == report.unchanged == 0
    assert any("a1" in n for n in report.notes)
    row = _trade_row(db)
    assert row["status"] == "placed"          # untouched: never guessed
    assert row["last_synced_utc"] is None


def test_missing_perm_id_skip_is_counted_not_just_noted(tmp_path):
    """Final-review I-1: a note alone let the sync ledger keep ok:true while
    a new order sat outside fill capture forever. The skip is now counted in
    its own bucket, which scripts/sync_ibkr_paper.py turns into ok:false."""
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, perm_id="")
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    report = reconcile_paper_book(str(db), venue)

    assert report.skipped_missing_perm_id == 1
    assert report.aborted is False


def test_unparseable_market_id_skip_is_not_counted_as_missing_perm_id(tmp_path):
    """The counter is specific: a row skipped for another reason (here an
    unreadable market_id) stays a note, not a missing-permId count."""
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, perm_id="111")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE trade_log SET market_id = 'garbage' WHERE order_id = 'a1'")
    conn.commit()
    conn.close()
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=222, order_id="V2")])

    report = reconcile_paper_book(str(db), venue)

    assert any("cannot read a conId" in n for n in report.notes)
    assert report.skipped_missing_perm_id == 0


def test_missing_perm_id_count_survives_a_mass_cancel_abort(tmp_path):
    """Like `examined`, the count is an observation, so an R8 abort (which
    zeroes every OUTCOME bucket) must not hide it."""
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="")
    _insert_order_row(db, order_id="b2", perm_id="222", con_id=OTHER_CON,
                      question_id="gtm-2026-12-9.9")
    venue = FakeSyncVenue(open_orders=[], positions=[])  # empty list -> R8 abort on b2

    report = reconcile_paper_book(str(db), venue)

    assert report.aborted is True
    assert report.skipped_missing_perm_id == 1


def test_foreign_venue_order_is_counted_and_never_written(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[
        _venue_order(perm_id=111),
        _venue_order(perm_id=987654321, order_id="V9", con_id=999),  # manual/foreign
    ])

    report = reconcile_paper_book(str(db), venue)

    assert report.unmatched_venue_orders == 1
    assert any("987654321" in n for n in report.notes)
    assert report.examined == 1 and report.unchanged == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0] == 1
    conn.close()
    assert _fill_events(db) == []


def test_venue_order_matching_a_closed_row_is_not_reported_as_foreign(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111", status="filled", filled_size=29.0)
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=111)])

    report = reconcile_paper_book(str(db), venue)

    assert report.examined == 0            # no candidate rows
    assert report.unmatched_venue_orders == 0  # it IS ours, just not open in the book


# ------------------------------------------------------------------ (f) idempotence
def test_second_run_with_unchanged_venue_state_adds_nothing(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0)])

    first = reconcile_paper_book(str(db), venue)
    assert first.filled == 1
    events_before = [tuple(e) for e in _fill_events(db)]
    positions_before = [tuple(p) for p in _positions(db)]
    row_before = _trade_row(db)

    second = reconcile_paper_book(str(db), venue)

    # The row is no longer a candidate: nothing to examine, nothing to write.
    assert second.examined == 0
    assert [tuple(e) for e in _fill_events(db)] == events_before
    assert [tuple(p) for p in _positions(db)] == positions_before
    row_after = _trade_row(db)
    assert (row_after["status"], row_after["filled_size"]) == (
        row_before["status"], row_before["filled_size"])


def test_rerunning_a_partial_adds_zero_fill_events(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    venue = FakeSyncVenue(open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)])

    reconcile_paper_book(str(db), venue)
    events_before = [tuple(e) for e in _fill_events(db)]
    row_before = _trade_row(db)

    report = reconcile_paper_book(str(db), venue)  # identical venue state

    assert report.unchanged == 1 and report.partially_filled == 0
    assert [tuple(e) for e in _fill_events(db)] == events_before
    row_after = _trade_row(db)
    assert (row_after["status"], row_after["filled_size"]) == (
        row_before["status"], row_before["filled_size"])
    # last_synced_utc is the one field a no-op run may refresh.
    assert row_after["last_synced_utc"] is not None


# ------------------------------------------------------------------ (g) cross-process
def test_reconciles_across_processes_with_no_in_memory_state(tmp_path):
    """The C1 red test: state must come from the DB, not from live objects.

    Run 1 records a partial fill. Every Python object is then dropped. Run 2
    -- fresh venue, fresh connection, nothing shared -- must still know that
    10 of the 29 contracts were already recorded and book only the remaining
    19 when the order completes.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)

    def _run_one():
        v = FakeSyncVenue(open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)])
        return reconcile_paper_book(str(db), v)

    assert _run_one().partially_filled == 1
    gc.collect()  # nothing from run 1 survives except the file on disk

    def _run_two():
        v = FakeSyncVenue(open_orders=[], positions=[_venue_position(quantity=29.0,
                                                                    avg_price=0.34)])
        return reconcile_paper_book(str(db), v)

    report = _run_two()

    assert report.filled == 1
    assert [(e["cum_filled"], e["fill_delta"]) for e in _fill_events(db)] == [
        (10.0, 10.0), (29.0, 19.0)]
    assert _trade_row(db)["status"] == "filled"
    assert _positions(db)[0]["size"] == pytest.approx(29.0)


def test_works_on_a_book_without_a_fill_events_table(tmp_path):
    """R2: a fresh book (pre-migration) still reconciles -- the table is created."""
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "fill_events" not in tables

    report = reconcile_paper_book(str(db), FakeSyncVenue(
        open_orders=[_venue_order(perm_id=111, filled=3.0, avg_price=0.34)]))

    assert report.partially_filled == 1
    assert len(_fill_events(db)) == 1


def test_a_pre_migration_book_is_refused_loudly(tmp_path):
    """A v1 book has no perm_id column: refuse before writing anything and
    name the migration, rather than half-reconciling on guessed identity.
    """
    from tests.test_trading.test_migrate_ibkr_paper_v2 import (
        V1_POSITIONS_DDL,
        V1_TRADE_LOG_DDL,
    )

    db = tmp_path / "ibkr_paper.db"
    conn = sqlite3.connect(db)
    conn.execute(V1_POSITIONS_DDL)
    conn.execute(V1_TRADE_LOG_DDL)
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="migrate_ibkr_paper_v2"):
        reconcile_paper_book(str(db), FakeSyncVenue())

    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "fill_events" not in tables  # refused before any schema change


# ------------------------------------------------------------------ (h) atomicity
def test_a_venue_failure_while_planning_writes_nothing(tmp_path):
    """Row a1 plans a partial fill, then row a2 forces the positions fetch,
    which raises. Planning precedes every write, so nothing can be half-done.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111")
    _insert_order_row(db, order_id="a2", perm_id="222", question_id="gtm-2026-12-1.5")
    venue = BoomVenue(open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)])

    with pytest.raises(RuntimeError, match="gateway dropped"):
        reconcile_paper_book(str(db), venue)

    a1 = _trade_row(db, "a1")
    assert a1["status"] == "placed"
    assert a1["filled_size"] == 0.0
    assert a1["last_synced_utc"] is None
    assert _trade_row(db, "a2")["status"] == "placed"
    assert _fill_events(db) == []
    assert _positions(db) == []
    # Even the DDL rolled back: schema and data share the one transaction.
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "fill_events" not in tables


def test_a_failure_while_writing_rolls_back_the_writes_already_made(tmp_path, monkeypatch):
    """The real single-transaction proof: a1's partial fill IS written, then
    a2's derived position blows up. Everything must vanish together.
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111")
    _insert_order_row(db, order_id="a2", perm_id="222", con_id=NO_CON, size=71.0,
                      price=0.14, outcome="NO", question_id="gtm-2026-12-1.5")

    def boom(*args, **kwargs):
        raise RuntimeError("disk gave up mid-write")

    monkeypatch.setattr("src.trading.reconcile._open_derived_position", boom)

    venue = FakeSyncVenue(
        open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)],
        positions=[_venue_position(con_id=NO_CON, quantity=71.0, avg_price=0.14, leg="NO")],
    )

    with pytest.raises(RuntimeError, match="disk gave up"):
        reconcile_paper_book(str(db), venue)

    a1 = _trade_row(db, "a1")
    assert a1["status"] == "placed"          # the partial that WAS written is gone
    assert a1["filled_size"] == 0.0
    assert a1["last_synced_utc"] is None
    assert _trade_row(db, "a2")["status"] == "placed"
    assert _fill_events(db) == []
    assert _positions(db) == []


def test_two_rows_reconcile_in_one_run(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111", con_id=YES_CON, size=29.0)
    _insert_order_row(db, order_id="a2", perm_id="222", con_id=NO_CON, size=71.0,
                      price=0.14, outcome="NO", question_id="gtm-2026-12-1.5")
    venue = FakeSyncVenue(
        open_orders=[_venue_order(perm_id=111, filled=10.0, avg_price=0.34)],
        positions=[_venue_position(con_id=NO_CON, quantity=71.0, avg_price=0.14, leg="NO")],
    )

    report = reconcile_paper_book(str(db), venue)

    assert report.examined == 2
    assert report.partially_filled == 1 and report.filled == 1
    assert _trade_row(db, "a1")["status"] == "partially_filled"
    assert _trade_row(db, "a2")["status"] == "filled"
    assert len(_positions(db)) == 1


def test_two_filled_rows_on_the_same_market_both_get_a_position(tmp_path):
    """The positions PK is (question_id, outcome, opened_at); two derived rows
    in one run must not collide (and must not silently drop one).
    """
    db = tmp_path / "ibkr_paper.db"
    _make_book(db)
    _insert_order_row(db, order_id="a1", perm_id="111", size=29.0)
    _insert_order_row(db, order_id="a2", perm_id="222", size=29.0)
    venue = FakeSyncVenue(open_orders=[],
                          positions=[_venue_position(quantity=58.0, avg_price=0.34)])

    report = reconcile_paper_book(str(db), venue)

    assert report.filled == 2
    rows = _positions(db)
    assert [r["order_id"] for r in rows] == ["a1", "a2"]
    assert len({r["opened_at"] for r in rows}) == 2  # distinct PKs, neither dropped
