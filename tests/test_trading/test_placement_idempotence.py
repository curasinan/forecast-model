"""Task 1.7: placement idempotence + persistent exposure seeding (C2).

A GTC order rests at the venue for days or weeks under the SAME book.
Nothing before this task stopped a re-run of the paper entrypoint from
placing that exact order a second time (idempotence: audit finding C2's
first half), and every fresh Python process started `RiskManager`
exposure back at zero even though the venue and the book already carry
real risk from a prior run (C2's second half). These tests pin both
fixes: `load_open_commitments` / `drop_already_committed`
(`src/trading/reconcile.py`, additive) and `RiskManager.seed_exposure`
(`src/trading/risk_manager.py`, additive).

Ground rule this suite enforces: dedup only ever SKIPS a would-be
duplicate placement. Nothing here may cancel anything found resting --
that is measurement state, not something to auto-clean.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.ids import MarketId
from src.domain.models import Market, Order, OrderRequest
from src.models.prediction import EdgeSignal
from src.trading.executor import TradeIntent, TradingExecutor
from src.trading.position_tracker import PositionTracker
from src.trading.reconcile import drop_already_committed, load_open_commitments
from src.trading.risk_manager import RiskLimits, RiskManager
from tests.test_trading.test_executor_venue import FakeVenue as _BaseFakeVenue

YES_CON = 712856754
NO_CON = 712856759


# ------------------------------------------------------------------ fakes
class FakeVenue(_BaseFakeVenue):
    """`test_executor_venue.FakeVenue` plus the venue-wide open-orders read
    `drop_already_committed` dedups against (IBKRAdapter.get_all_open_orders,
    reqAllOpenOrders semantics: every currently-resting order). This fake
    has only one client, so "resting" is just "still open" in its own
    order book.
    """

    def get_all_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status == "open"]


def _edge(edge_pct=0.15, model_prob=0.65, market_prob=0.50, confidence=0.8) -> EdgeSignal:
    return EdgeSignal(question_id="gtm-2026-12-1.2", model_prob=model_prob, market_prob=market_prob,
                      edge_pct=edge_pct, expected_value=0.15, kelly_fraction=0.10,
                      confidence=confidence)


def _ibkr_market(days: int = 120) -> Market:
    return Market(
        market_id=MarketId.ibkr(YES_CON), no_id=MarketId.ibkr(NO_CON),
        symbol="GTM", title="GTM Dec-2026 > 1.2", strike=1.2, size_increment=1.0,
        tick_size=0.01,
        scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=days),
    )


# ------------------------------------------------------------------ db helpers
def _make_book(path) -> None:
    """Create a v2-shaped paper book (PositionTracker owns the DDL)."""
    PositionTracker(db_path=str(path))


def _insert_trade_row(path, *, order_id: str, question_id: str, market_id: str,
                      cost_usd: float, status: str, price: float = 0.34,
                      size: float | None = None, outcome: str = "YES") -> None:
    if size is None:
        size = round(cost_usd / price, 4)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd, status,
            reason, created_at, filled_at, market_id, venue, venue_order_id,
            filled_size, perm_id, last_synced_utc)
           VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, 'edge', '2026-08-25T10:00:00', NULL,
                   ?, 'ibkr', 'V1', 0.0, '', NULL)""",
        (order_id, question_id, outcome, price, size, cost_usd, status, market_id),
    )
    conn.commit()
    conn.close()


def _insert_position_row(path, *, order_id: str, question_id: str, market_id: str,
                         cost_basis: float, entry_price: float = 0.34,
                         size: float | None = None, outcome: str = "YES",
                         opened_at: str = "2026-08-20T10:00:00+00:00") -> None:
    if size is None:
        size = round(cost_basis / entry_price, 4)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """INSERT INTO positions
           (question_id, outcome, entry_price, size, cost_basis, opened_at,
            market_id, venue, order_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'ibkr', ?)""",
        (question_id, outcome, entry_price, size, cost_basis, opened_at, market_id, order_id),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ (a) end-to-end idempotence
def test_rerun_with_resting_orders_places_nothing(tmp_path):
    db = str(tmp_path / "p.db")
    venue = FakeVenue(read_only=False)
    ex1 = TradingExecutor(venue=venue, bankroll=100.0, db_path=db, simulate=False)
    ex1.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    n_requests_after_first = len(venue.requests)

    # Fresh process: new executor, same DB, same venue state, same intent.
    ex2 = TradingExecutor(venue=venue, bankroll=100.0, db_path=db, simulate=False)
    intents = [TradeIntent(edge=_edge(), market=_ibkr_market())]
    kept = drop_already_committed(intents, venue.get_all_open_orders(),
                                  load_open_commitments(db))
    ex2.risk_manager.seed_exposure(load_open_commitments(db)["total_usd"])
    rep = ex2.execute_intents(kept, mode="auto")

    assert kept == [] and rep.trades_executed == 0
    assert len(venue.requests) == n_requests_after_first


def test_exposure_seeded_from_db(tmp_path):
    rm = RiskManager(limits=RiskLimits(max_total_exposure_usd=50.0), bankroll=100.0)
    rm.seed_exposure(29.64)
    res = rm.check_trade(edge=_edge(), proposed_size_usd=30.0)
    assert res.adjusted_size_usd <= 50.0 - 29.64 + 1e-9


# ------------------------------------------------------------------ (c) load_open_commitments
def test_missing_db_file_returns_the_empty_shape(tmp_path):
    commitments = load_open_commitments(str(tmp_path / "does_not_exist.db"))
    assert commitments == {"total_usd": 0.0, "by_market": {}, "by_group": {}}


def test_load_open_commitments_avoids_double_counting_a_partial_fill(tmp_path):
    """A partially_filled row's cost_usd is the FULL intended commitment
    (the whole order, not just what has filled so far). Its own partial
    position row must not be added on top of that -- 9.84, not 12.24.
    A filled row is no longer a trade_log commitment, so it contributes
    only through its (now fully-sized) position: 9.86.
    """
    db = tmp_path / "p.db"
    _make_book(db)
    _insert_trade_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                      market_id="ibkr:712856754", cost_usd=9.84, status="partially_filled")
    _insert_position_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                         market_id="ibkr:712856754", cost_basis=2.40)
    _insert_trade_row(db, order_id="a2", question_id="gtm:2026-12:1.5",
                      market_id="ibkr:712856759", cost_usd=9.86, status="filled")
    _insert_position_row(db, order_id="a2", question_id="gtm:2026-12:1.5",
                         market_id="ibkr:712856759", cost_basis=9.86)

    commitments = load_open_commitments(str(db))

    assert commitments["total_usd"] == pytest.approx(9.84 + 9.86)
    assert commitments["by_market"]["ibkr:712856754"] == pytest.approx(9.84)
    assert commitments["by_market"]["ibkr:712856759"] == pytest.approx(9.86)
    assert commitments["by_group"]["gtm:2026-12"] == pytest.approx(9.84 + 9.86)


def test_load_open_commitments_ignores_cancelled_and_rejected_rows(tmp_path):
    db = tmp_path / "p.db"
    _make_book(db)
    _insert_trade_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                      market_id="ibkr:712856754", cost_usd=9.84, status="cancelled")
    _insert_trade_row(db, order_id="a2", question_id="gtm:2026-12:1.5",
                      market_id="ibkr:712856759", cost_usd=9.86, status="rejected")

    commitments = load_open_commitments(str(db))

    assert commitments == {"total_usd": 0.0, "by_market": {}, "by_group": {}}


def test_load_open_commitments_group_key_drops_the_trailing_strike_segment(tmp_path):
    db = tmp_path / "p.db"
    _make_book(db)
    _insert_trade_row(db, order_id="a1", question_id="gtm:2026-12:1.3",
                      market_id="ibkr:1", cost_usd=5.0, status="placed")

    commitments = load_open_commitments(str(db))

    assert commitments["by_group"] == {"gtm:2026-12": 5.0}


# ------------------------------------------------------------------ (d) drop_already_committed
def test_drop_already_committed_drops_on_a_resting_venue_order_only(tmp_path):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)  # YES: edge_pct=0.15 > 0
    resting = [Order(
        order_id="V1",
        request=OrderRequest(market_id=market.market_id, side="BUY", leg="YES",
                             quantity=29.0, order_type="LMT", limit_price=0.34, tif="GTC"),
        status="open",
    )]
    empty_commitments = {"total_usd": 0.0, "by_market": {}, "by_group": {}}

    kept = drop_already_committed([intent], resting, empty_commitments)

    assert kept == []


def test_drop_already_committed_drops_on_a_db_commitment_only(tmp_path):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)
    commitments = {"total_usd": 9.86, "by_market": {f"ibkr:{YES_CON}": 9.86}, "by_group": {}}

    kept = drop_already_committed([intent], [], commitments)

    assert kept == []


def test_drop_already_committed_keeps_an_uncommitted_intent(tmp_path):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)
    empty_commitments = {"total_usd": 0.0, "by_market": {}, "by_group": {}}

    kept = drop_already_committed([intent], [], empty_commitments)

    assert kept == [intent]


def test_drop_already_committed_dedups_the_no_leg_on_its_own_con_id(tmp_path):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(edge_pct=-0.15, model_prob=0.35), market=market, no_price=0.65)
    # A resting order on the YES leg must NOT protect the NO leg -- the two
    # are separate IBKR contracts.
    resting = [Order(
        order_id="V1",
        request=OrderRequest(market_id=market.market_id, side="BUY", leg="YES",
                             quantity=29.0, order_type="LMT", limit_price=0.34, tif="GTC"),
        status="open",
    )]
    empty_commitments = {"total_usd": 0.0, "by_market": {}, "by_group": {}}
    kept = drop_already_committed([intent], resting, empty_commitments)
    assert kept == [intent]  # YES-leg order does not cover the NO leg

    resting_no = [Order(
        order_id="V2",
        request=OrderRequest(market_id=market.no_id, side="BUY", leg="NO",
                             quantity=71.0, order_type="LMT", limit_price=0.65, tif="GTC"),
        status="open",
    )]
    assert drop_already_committed([intent], resting_no, empty_commitments) == []


def test_drop_already_committed_drops_a_no_intent_with_no_resolvable_no_id(caplog):
    market = Market(market_id=MarketId.ibkr(YES_CON), no_id=None)
    intent = TradeIntent(edge=_edge(edge_pct=-0.15, model_prob=0.35), market=market)
    empty_commitments = {"total_usd": 0.0, "by_market": {}, "by_group": {}}

    with caplog.at_level(logging.WARNING, logger="src.trading.reconcile"):
        kept = drop_already_committed([intent], [], empty_commitments)

    assert kept == []
    assert any("NO" in r.message and "gtm-2026-12-1.2" in r.message for r in caplog.records)


def test_drop_already_committed_tolerates_an_unparseable_by_market_key(tmp_path):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)
    commitments = {"total_usd": 5.0, "by_market": {"not-a-market-id": 5.0}, "by_group": {}}

    kept = drop_already_committed([intent], [], commitments)

    assert kept == [intent]


def test_drop_already_committed_logs_the_reason_at_warning(caplog):
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)
    commitments = {"total_usd": 9.86, "by_market": {f"ibkr:{YES_CON}": 9.86}, "by_group": {}}

    with caplog.at_level(logging.WARNING, logger="src.trading.reconcile"):
        drop_already_committed([intent], [], commitments)

    assert any("gtm-2026-12-1.2" in r.message for r in caplog.records)


def test_drop_already_committed_never_touches_the_resting_orders_it_is_given(tmp_path):
    """Ground rule 1: dedup only skips placement. It must never mutate or
    cancel anything it was handed."""
    market = _ibkr_market()
    intent = TradeIntent(edge=_edge(), market=market)
    resting = [Order(
        order_id="V1",
        request=OrderRequest(market_id=market.market_id, side="BUY", leg="YES",
                             quantity=29.0, order_type="LMT", limit_price=0.34, tif="GTC"),
        status="open",
    )]
    before = resting[0].model_copy(deep=True)

    drop_already_committed([intent], resting, {"total_usd": 0.0, "by_market": {}, "by_group": {}})

    assert resting[0] == before


# ------------------------------------------------------------------ (e) RiskManager.seed_exposure
def test_seed_exposure_raises_total_exposure_to_the_seeded_value():
    rm = RiskManager(bankroll=100.0)
    assert rm._total_exposure == 0.0
    rm.seed_exposure(29.64)
    assert rm._total_exposure == pytest.approx(29.64)


def test_seed_exposure_never_lowers_exposure_already_observed():
    """max, not assignment: a re-seed must not claw back exposure this
    process has already added for trades placed since the last seed."""
    rm = RiskManager(bankroll=100.0)
    rm.add_exposure(40.0)
    rm.seed_exposure(10.0)
    assert rm._total_exposure == pytest.approx(40.0)


def test_seed_exposure_is_idempotent_across_reseeds_of_the_same_value():
    rm = RiskManager(bankroll=100.0)
    rm.seed_exposure(29.64)
    rm.seed_exposure(29.64)
    assert rm._total_exposure == pytest.approx(29.64)
