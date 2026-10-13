"""Correlated-group concentration cap (H5, audit finding)."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.ids import MarketId
from src.domain.models import Market
from src.models.prediction import EdgeSignal
from src.trading.executor import (
    ExecutionReport,
    TradeDecision,
    TradeIntent,
    TradingExecutor,
    render_group_commitments,
)
from src.trading.executor import _group_key as executor_group_key
from src.trading.order_manager import OrderSide, OrderStatus, TradeOrder
from src.trading.position_tracker import PositionTracker
from src.trading.reconcile import _group_key as reconcile_group_key
from src.trading.reconcile import load_open_commitments
from src.trading.risk_manager import RiskCheckResult, RiskLimits, RiskManager
from tests.test_trading.test_executor_venue import FakeVenue

YES_CON = 712856754
NO_CON = 712856759


def _edge(question_id: str = "gtm:2026-12:1.2", edge_pct=0.15, model_prob=0.65,
          market_prob=0.50, confidence=0.8, kelly_fraction=0.10) -> EdgeSignal:
    return EdgeSignal(question_id=question_id, model_prob=model_prob, market_prob=market_prob,
                      edge_pct=edge_pct, expected_value=0.15, kelly_fraction=kelly_fraction,
                      confidence=confidence)


def _ibkr_market(days: int = 120) -> Market:
    return Market(
        market_id=MarketId.ibkr(YES_CON), no_id=MarketId.ibkr(NO_CON),
        symbol="GTM", title="GTM Dec-2026 > 1.2", strike=1.2, size_increment=1.0,
        tick_size=0.01,
        scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=days),
    )


class TestGroupCapOnRiskManager:
    GROUP = "gtm:2026-12"

    def _rm(self, max_group_usd: float = 15.0, **limit_kw) -> RiskManager:
        return RiskManager(
            limits=RiskLimits(max_position_usd=10.0, max_group_usd=max_group_usd,
                              max_total_exposure_usd=1000.0, **limit_kw),
            bankroll=1000.0,
        )

    def test_first_10_dollar_intent_in_a_group_is_approved_in_full(self):
        rm = self._rm()
        r1 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        assert r1.approved is True
        assert r1.adjusted_size_usd == pytest.approx(10.0)

    def test_second_10_dollar_intent_is_clipped_to_remaining_headroom(self):
        rm = self._rm()
        r1 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        rm.add_exposure(r1.adjusted_size_usd, group_key=self.GROUP)

        r2 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)

        assert r2.approved is True
        assert r2.adjusted_size_usd <= 5.0
        assert r2.adjusted_size_usd == pytest.approx(5.0)

    def test_third_intent_is_rejected_naming_the_group(self):
        rm = self._rm()
        r1 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        rm.add_exposure(r1.adjusted_size_usd, group_key=self.GROUP)
        r2 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        rm.add_exposure(r2.adjusted_size_usd, group_key=self.GROUP)

        r3 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)

        assert r3.approved is False
        assert r3.adjusted_size_usd == 0.0
        assert self.GROUP in r3.reason

    def test_seeded_group_at_29_64_rejects_any_further_intent(self):
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 29.64})

        result = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)

        assert result.approved is False
        assert self.GROUP in result.reason

    def test_different_group_is_unaffected_by_a_seeded_or_full_group(self):
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 29.64})

        other = rm.check_trade(_edge(question_id="gtm:2026-11:1.0"), proposed_size_usd=10.0,
                               group_key="gtm:2026-11")

        assert other.approved is True
        assert other.adjusted_size_usd == pytest.approx(10.0)

    def test_group_key_empty_is_byte_identical_to_no_group_key_argument(self):
        rm_a = self._rm()
        rm_b = self._rm()

        r_explicit_empty = rm_a.check_trade(_edge(), proposed_size_usd=10.0, group_key="")
        r_omitted = rm_b.check_trade(_edge(), proposed_size_usd=10.0)

        assert r_explicit_empty.approved == r_omitted.approved
        assert r_explicit_empty.adjusted_size_usd == r_omitted.adjusted_size_usd
        assert r_explicit_empty.reason == r_omitted.reason
        assert r_explicit_empty.checks_passed == r_omitted.checks_passed
        assert r_explicit_empty.checks_failed == r_omitted.checks_failed

    def test_group_key_empty_never_touches_group_exposure_even_after_seeding(self):
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 29.64})

        result = rm.check_trade(_edge(question_id="not-a-gtm-id"), proposed_size_usd=10.0)

        assert result.approved is True
        assert result.adjusted_size_usd == pytest.approx(10.0)

    def test_group_cap_rejection_leaves_adjusted_size_at_zero(self):
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 15.0})
        result = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        assert result.approved is False
        assert result.adjusted_size_usd == 0.0

    def test_group_headroom_of_exactly_the_cap_allows_full_size(self):
        rm = self._rm()
        result = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        assert result.approved is True
        assert result.adjusted_size_usd == pytest.approx(10.0)


class TestSeedGroupExposureIdempotence:
    """Mirrors the seed_exposure idempotence suite exactly, for the group-scoped ledger."""

    def test_seed_group_exposure_raises_the_named_group_to_the_seeded_value(self):
        rm = RiskManager(bankroll=100.0)
        assert rm._group_exposure == {}
        rm.seed_group_exposure({"gtm:2026-12": 29.64})
        assert rm._group_exposure["gtm:2026-12"] == pytest.approx(29.64)

    def test_seed_group_exposure_never_lowers_exposure_already_added(self):
        rm = RiskManager(bankroll=100.0)
        rm.add_exposure(10.0, group_key="gtm:2026-12")
        rm.add_exposure(10.0, group_key="gtm:2026-12")  # this run's own adds: $20
        rm.seed_group_exposure({"gtm:2026-12": 5.0})  # a stale/smaller seed
        assert rm._group_exposure["gtm:2026-12"] == pytest.approx(20.0)

    def test_seed_group_exposure_is_idempotent_across_reseeds_of_the_same_value(self):
        rm = RiskManager(bankroll=100.0)
        rm.seed_group_exposure({"gtm:2026-12": 29.64})
        rm.seed_group_exposure({"gtm:2026-12": 29.64})
        assert rm._group_exposure["gtm:2026-12"] == pytest.approx(29.64)

    def test_seed_group_exposure_does_not_touch_unrelated_groups(self):
        rm = RiskManager(bankroll=100.0)
        rm.add_exposure(5.0, group_key="gtm:2026-11")
        rm.seed_group_exposure({"gtm:2026-12": 29.64})
        assert rm._group_exposure["gtm:2026-11"] == pytest.approx(5.0)
        assert rm._group_exposure["gtm:2026-12"] == pytest.approx(29.64)

    def test_add_exposure_without_group_key_still_moves_total_exposure(self):
        rm = RiskManager(bankroll=100.0)
        rm.add_exposure(40.0)
        assert rm._total_exposure == pytest.approx(40.0)
        assert rm._group_exposure == {}


class TestExecutorGroupKeyDerivation:
    def test_gtm_question_id_derives_the_month_group(self):
        assert executor_group_key("gtm:2026-12:1.2") == "gtm:2026-12"
        assert executor_group_key("gtm:2026-12:1.3") == "gtm:2026-12"
        assert executor_group_key("gtm:2026-12:1.4") == "gtm:2026-12"

    def test_non_gtm_question_id_gets_the_empty_group(self):
        assert executor_group_key("gtm-2026-12-1.2") == ""  # legacy dash id, no "gtm:" prefix
        assert executor_group_key("0xabc123") == ""  # Polymarket-style id
        assert executor_group_key("") == ""

    def test_executor_group_key_matches_reconcile_group_key_for_every_gtm_id(self):
        for qid in ("gtm:2026-12:1.2", "gtm:2026-12:1.3", "gtm:2027-01:0.9",
                    "gtm:2026-12", "gtm:2026-12:-0.5"):
            assert executor_group_key(qid) == reconcile_group_key(qid)


class TestExecutorThreadsGroupCap:
    def _executor(self, tmp_path, max_group_usd: float = 15.0) -> TradingExecutor:
        return TradingExecutor(
            venue=FakeVenue(read_only=True), bankroll=1000.0, db_path=str(tmp_path / "t.db"),
            risk_limits=RiskLimits(max_position_usd=10.0, max_group_usd=max_group_usd,
                                   max_total_exposure_usd=1000.0),
        )

    def _intent(self, strike: str) -> TradeIntent:
        return TradeIntent(edge=_edge(question_id=f"gtm:2026-12:{strike}"), market=_ibkr_market(),
                           title=f"GTM Dec-2026 > {strike}")

    def test_two_orders_in_one_group_the_second_is_clipped_by_the_group_cap(self, tmp_path):
        ex = self._executor(tmp_path)
        rep = ex.execute_intents([self._intent("1.2"), self._intent("1.3")], mode="auto")

        d1, d2 = rep.decisions
        assert d1.executed and d1.order.cost_usd == pytest.approx(10.0, abs=0.02)
        assert d2.executed
        assert d2.order.cost_usd <= 5.0 + 1e-6

    def test_third_order_in_the_same_group_is_rejected(self, tmp_path):
        ex = self._executor(tmp_path)
        rep = ex.execute_intents(
            [self._intent("1.2"), self._intent("1.3"), self._intent("1.4")], mode="auto",
        )

        assert rep.trades_executed == 2
        assert rep.trades_rejected == 1
        d3 = rep.decisions[2]
        assert d3.executed is False
        assert "gtm:2026-12" in d3.reason

    def test_a_different_group_on_the_same_run_is_not_capped_by_the_first(self, tmp_path):
        ex = self._executor(tmp_path)
        rep = ex.execute_intents(
            [self._intent("1.2"), self._intent("1.3"),
             TradeIntent(edge=_edge(question_id="gtm:2026-11:1.0"), market=_ibkr_market(),
                        title="GTM Nov-2026 > 1.0")],
            mode="auto",
        )
        assert rep.trades_executed == 3  # the third is a different print entirely

    def test_non_gtm_intent_is_never_group_capped(self, tmp_path):
        ex = self._executor(tmp_path, max_group_usd=1.0)  # a cap so tight ANY gtm order would fail
        rep = ex.execute_intents(
            [TradeIntent(edge=_edge(question_id="gtm-legacy-dash-id"), market=_ibkr_market())],
            mode="auto",
        )
        assert rep.trades_executed == 1
        assert rep.decisions[0].order.cost_usd == pytest.approx(10.0, abs=0.02)

    def test_seed_group_exposure_makes_a_fresh_executor_reject_immediately(self, tmp_path):
        ex = self._executor(tmp_path)
        ex.risk_manager.seed_group_exposure({"gtm:2026-12": 29.64})

        rep = ex.execute_intents([self._intent("1.2")], mode="auto")

        assert rep.trades_executed == 0
        assert rep.trades_rejected == 1
        assert "gtm:2026-12" in rep.decisions[0].reason

    def test_group_cap_never_cancels_anything(self, tmp_path):
        """Ground rule: the cap only blocks NEW placement."""
        venue = FakeVenue(read_only=False)
        ex = TradingExecutor(
            venue=venue, bankroll=1000.0, db_path=str(tmp_path / "t.db"), simulate=False,
            risk_limits=RiskLimits(max_position_usd=10.0, max_group_usd=15.0,
                                   max_total_exposure_usd=1000.0),
        )
        ex.execute_intents(
            [self._intent("1.2"), self._intent("1.3"), self._intent("1.4")], mode="auto",
        )
        assert all(o.status != "cancelled" for o in venue._orders.values())

    def test_execute_intents_evaluates_in_the_given_order_no_edge_sort(self, tmp_path):
        ex = self._executor(tmp_path)
        low_edge_first = TradeIntent(
            edge=_edge(question_id="gtm:2026-12:1.2", edge_pct=0.06), market=_ibkr_market(),
        )
        high_edge_second = TradeIntent(
            edge=_edge(question_id="gtm:2026-12:1.3", edge_pct=0.40), market=_ibkr_market(),
        )
        rep = ex.execute_intents([low_edge_first, high_edge_second], mode="auto")
        d_low, d_high = rep.decisions
        assert d_low.executed and d_low.order.cost_usd == pytest.approx(10.0, abs=0.02)
        assert d_high.executed and d_high.order.cost_usd <= 5.0 + 1e-6


def _make_book(path) -> None:
    PositionTracker(db_path=str(path))


def _insert_placed_row(path, *, order_id: str, question_id: str, market_id: str,
                       size: float, price: float) -> None:
    cost_usd = round(size * price, 2)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd, status,
            reason, created_at, filled_at, market_id, venue, venue_order_id,
            filled_size, perm_id, last_synced_utc)
           VALUES (?, ?, 'YES', 'BUY', ?, ?, ?, 'placed', 'edge',
                   '2026-08-25T10:00:00', NULL, ?, 'ibkr', 'V1', 0.0, '', NULL)""",
        (order_id, question_id, price, size, cost_usd, market_id),
    )
    conn.commit()
    conn.close()


def _merged_group_counts(commitments: dict, report: ExecutionReport) -> dict[str, int]:
    counts = dict(commitments["by_group_counts"])
    for key, n in report.group_order_counts().items():
        counts[key] = counts.get(key, 0) + n
    return counts


class TestRenderGroupCommitments:
    def test_acceptance_format_three_strikes_one_correlated_bet(self):
        text = render_group_commitments({"gtm:2026-12": 29.64}, {"gtm:2026-12": 3})
        assert "gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)" in text

    def test_singular_order_is_not_pluralized(self):
        """Ruling item 4's in-passing pluralization fix: "1 orders" -> "1 order"."""
        text = render_group_commitments({"gtm:2026-12": 5.0}, {"gtm:2026-12": 1})
        assert "gtm:2026-12 = $5.00 (1 order, 1 correlated bet)" in text
        assert "1 orders" not in text

    def test_empty_group_exposure_renders_nothing(self):
        assert render_group_commitments({}, {}) == ""

    def test_two_distinct_groups_each_get_their_own_line(self):
        text = render_group_commitments(
            {"gtm:2026-12": 10.0, "gtm:2026-11": 7.5},
            {"gtm:2026-12": 1, "gtm:2026-11": 1},
        )
        assert "gtm:2026-12 = $10.00 (1 order, 1 correlated bet)" in text
        assert "gtm:2026-11 = $7.50 (1 order, 1 correlated bet)" in text

    def test_a_group_missing_from_group_counts_renders_zero_orders(self):
        text = render_group_commitments({"gtm:2026-12": 5.0}, {})
        assert "gtm:2026-12 = $5.00 (0 orders, 1 correlated bet)" in text

    def test_header_states_it_is_the_committed_book_seeded_plus_this_run(self):
        text = render_group_commitments({"gtm:2026-12": 1.0}, {"gtm:2026-12": 1})
        assert "committed book" in text.lower()
        assert "seed" in text.lower()
        assert "this run" in text.lower()


class TestSummaryDoesNotRenderGroupsFromDecisionsAlone:
    def _decision(self, question_id: str, cost_usd: float) -> TradeDecision:
        order = TradeOrder(
            order_id="x", question_id=question_id, market_id="ibkr:1", side=OrderSide.BUY,
            outcome="YES", price=0.5, size=cost_usd / 0.5, cost_usd=cost_usd,
            status=OrderStatus.SIMULATED,
        )
        return TradeDecision(
            question_id=question_id, question_title=question_id, edge_pct=0.15,
            direction="YES underpriced",
            risk_check=RiskCheckResult(approved=True, reason="APPROVED", adjusted_size_usd=cost_usd),
            order=order, executed=True, reason="APPROVED",
        )

    def test_summary_alone_never_prints_a_correlated_bet_line(self):
        report = ExecutionReport(decisions=[
            self._decision("gtm:2026-12:1.2", 9.86),
            self._decision("gtm:2026-12:1.3", 9.84),
            self._decision("gtm:2026-12:1.4", 9.94),
        ])
        report.trades_executed = 3
        assert "correlated bet" not in report.summary()

    def test_group_order_counts_reflects_this_runs_own_decisions(self):
        report = ExecutionReport(decisions=[
            self._decision("gtm:2026-12:1.2", 9.86),
            self._decision("gtm:2026-12:1.3", 9.84),
            self._decision("gtm:2026-11:1.0", 5.0),
        ])
        assert report.group_order_counts() == {"gtm:2026-12": 2, "gtm:2026-11": 1}

    def test_group_order_counts_omits_ungrouped_and_unexecuted(self):
        skipped = self._decision("gtm:2026-12:1.2", 10.0)
        skipped.executed = False
        ungrouped = self._decision("0xabc123", 10.0)
        report = ExecutionReport(decisions=[skipped, ungrouped])
        assert report.group_order_counts() == {}


class TestGroupExposureAccessor:
    def test_group_exposure_combines_seed_and_this_runs_adds(self):
        rm = RiskManager(bankroll=1000.0)
        rm.seed_group_exposure({"gtm:2026-12": 8.0})
        rm.add_exposure(7.0, group_key="gtm:2026-12")
        assert rm.group_exposure() == {"gtm:2026-12": pytest.approx(15.0)}

    def test_group_exposure_returns_a_copy_not_the_live_dict(self):
        rm = RiskManager(bankroll=1000.0)
        rm.add_exposure(5.0, group_key="gtm:2026-12")
        snapshot = rm.group_exposure()
        snapshot["gtm:2026-12"] = 999.0
        assert rm.group_exposure()["gtm:2026-12"] == pytest.approx(5.0)

    def test_group_exposure_empty_by_default(self):
        assert RiskManager(bankroll=1000.0).group_exposure() == {}


class TestGroupCommitmentsBehaviorAgainstADB:
    def test_the_real_three_strike_book_renders_the_acceptance_line(self, tmp_path):
        db = tmp_path / "p.db"
        _make_book(db)
        _insert_placed_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                           market_id="ibkr:1", size=29.0, price=0.34)
        _insert_placed_row(db, order_id="a2", question_id="gtm:2026-12:1.3",
                           market_id="ibkr:2", size=41.0, price=0.24)
        _insert_placed_row(db, order_id="a3", question_id="gtm:2026-12:1.4",
                           market_id="ibkr:3", size=71.0, price=0.14)

        commitments = load_open_commitments(str(db))
        rm = RiskManager(bankroll=1000.0)
        rm.seed_exposure(commitments["total_usd"])
        rm.seed_group_exposure(commitments["by_group"])

        # This run's own count is zero -- nothing new placed in the group.
        group_counts = dict(commitments["by_group_counts"])

        text = render_group_commitments(rm.group_exposure(), group_counts)

        assert "gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)" in text

    def test_a_new_placement_this_run_adds_to_the_seeded_book_not_shown_alone(self, tmp_path):
        db = tmp_path / "p.db"
        _make_book(db)
        _insert_placed_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                           market_id="ibkr:1", size=8.0, price=1.0)

        commitments = load_open_commitments(str(db))
        rm = RiskManager(bankroll=1000.0)
        rm.seed_exposure(commitments["total_usd"])
        rm.seed_group_exposure(commitments["by_group"])

        # THIS run places a second order in the same group.
        rm.add_exposure(7.0, group_key="gtm:2026-12")
        group_counts = dict(commitments["by_group_counts"])
        group_counts["gtm:2026-12"] = group_counts.get("gtm:2026-12", 0) + 1

        text = render_group_commitments(rm.group_exposure(), group_counts)

        assert "gtm:2026-12 = $15.00 (2 orders, 1 correlated bet)" in text
        assert "$7.00" not in text

    def test_end_to_end_through_a_real_executor_run(self, tmp_path):
        db = tmp_path / "p.db"
        _make_book(db)
        _insert_placed_row(db, order_id="a1", question_id="gtm:2026-12:1.2",
                           market_id="ibkr:99", size=8.0, price=1.0)
        commitments = load_open_commitments(str(db))

        ex = TradingExecutor(
            venue=FakeVenue(read_only=True), bankroll=1000.0, db_path=str(tmp_path / "t.db"),
            risk_limits=RiskLimits(max_position_usd=10.0, max_group_usd=15.0,
                                   max_total_exposure_usd=1000.0),
        )
        ex.risk_manager.seed_exposure(commitments["total_usd"])
        ex.risk_manager.seed_group_exposure(commitments["by_group"])

        # $7 of headroom left (15 - 8) in the same group.
        intent = TradeIntent(edge=_edge(question_id="gtm:2026-12:1.3"), market=_ibkr_market())
        report = ex.execute_intents([intent], mode="auto")

        group_counts = _merged_group_counts(commitments, report)
        text = render_group_commitments(ex.risk_manager.group_exposure(), group_counts)

        assert report.trades_executed == 1
        assert "gtm:2026-12 = $15.00 (2 orders, 1 correlated bet)" in text


# Section D: docstring-carried decisions (known limitation, verbatim).
class TestDocumentedLimitation:
    def test_risk_limits_docstring_states_the_cross_symbol_known_limitation(self):
        doc = RiskLimits.__doc__ or ""
        assert "operator-approved 2026-08-25" in doc
        assert "CROSS-SYMBOL" in doc or "cross-symbol" in doc.lower()
        assert "GTTA" in doc and "GTTM" in doc

    def test_check_trade_docstring_states_ordering_semantics(self):
        doc = RiskManager.check_trade.__doc__ or ""
        assert "order" in doc.lower()

    def test_execute_intents_docstring_states_it_does_not_sort(self):
        doc = inspect.getdoc(TradingExecutor.execute_intents) or ""
        assert "does not sort" in doc or "not sort" in doc


def test_run_ibkr_paper_seeds_group_exposure_next_to_total_exposure_inside_the_lock():
    import scripts.run_ibkr_paper as mod

    source = inspect.getsource(mod.main)
    lock_pos = source.index("with acquire_db_lock(")
    seed_total_pos = source.index("seed_exposure(")
    seed_group_pos = source.index("seed_group_exposure(")

    assert seed_total_pos > lock_pos
    assert seed_group_pos > lock_pos
    assert "commitments[\"by_group\"]" in source or "commitments['by_group']" in source
    _assert_seeds_before_execute(source)


def _assert_seeds_before_execute(source: str) -> None:
    execute_pos = source.index("execute_intents(")
    assert source.index("seed_exposure(") < execute_pos, (
        "seed_exposure( must precede execute_intents( in run_ibkr_paper.main()"
    )
    assert source.index("seed_group_exposure(") < execute_pos, (
        "seed_group_exposure( must precede execute_intents( in run_ibkr_paper.main() -- "
        "otherwise the group cap cannot see the DB-committed book during placement"
    )


def test_seed_before_execute_pin_catches_a_swapped_order():
    good = (
        "executor.risk_manager.seed_exposure(total)\n"
        "executor.risk_manager.seed_group_exposure(groups)\n"
        "report = executor.execute_intents(intents)\n"
    )
    _assert_seeds_before_execute(good)
    swapped = (
        "executor.risk_manager.seed_exposure(total)\n"
        "report = executor.execute_intents(intents)\n"
        "executor.risk_manager.seed_group_exposure(groups)\n"
    )
    with pytest.raises(AssertionError, match="seed_group_exposure"):
        _assert_seeds_before_execute(swapped)


def test_run_ibkr_paper_renders_the_committed_book_from_the_ledger_not_the_report_alone():
    import scripts.run_ibkr_paper as mod

    source = inspect.getsource(mod.main)
    lock_pos = source.index("with acquire_db_lock(")
    report_pos = source.index("executor.execute_intents(")
    render_pos = source.index("render_group_commitments(")
    group_exposure_pos = source.index("executor.risk_manager.group_exposure()")
    by_group_counts_pos = source.index("commitments[\"by_group_counts\"]")

    for marker_pos in (report_pos, render_pos, group_exposure_pos, by_group_counts_pos):
        assert marker_pos > lock_pos
    assert render_pos > report_pos
    assert group_exposure_pos > report_pos
