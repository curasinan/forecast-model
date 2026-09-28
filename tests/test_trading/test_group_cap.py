"""Task 3.6: correlated-group concentration cap (H5, audit finding).

The three resting GTC orders in `gtm:2026-12` ($29.64 total) are all
strikes of the SAME December-2026 NOAA print -- one correlated bet,
uncapped as a group before this task. `RiskLimits.max_group_usd` (H5,
operator-approved value $15.00) caps committed notional per correlated
print; `RiskManager.seed_group_exposure` seeds it from the DB so a
restart does not forget what a prior process (or the three orders
already resting) committed.

Ground rules this suite pins (see the Task 3.6 brief and the two named
bug-ledger lessons it calls out):

- The three resting orders are NEVER touched here -- this cap only ever
  blocks NEW placement (`check_trade` rejects/clips; nothing calls
  cancel).
- `group_key=""` (the default, and what every non-`gtm:*` question_id
  gets from `src.trading.executor._group_key`) must leave every existing
  check and the adjusted size byte-identical to before this cap existed
  (regress note).
- The group ledger must count SEEDED commitments (the DB's open book)
  together with THIS RUN's own accepted intents against the cap
  (missing-position-dedup lesson) -- not either one alone.
- Headroom is consumed strictly in the order intents/checks are
  evaluated (order-dependent-cap-scaling lesson) -- this suite does not
  expect any edge-aware reordering, and pins that `execute_intents` does
  not sort.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.ids import MarketId
from src.domain.models import Market
from src.models.prediction import EdgeSignal
from src.trading.executor import ExecutionReport, TradeDecision, TradeIntent, TradingExecutor
from src.trading.executor import _group_key as executor_group_key
from src.trading.order_manager import OrderSide, OrderStatus, TradeOrder
from src.trading.reconcile import _group_key as reconcile_group_key
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


# ===================================================================
# Section A: RiskManager.check_trade / add_exposure -- the brief's
# verbatim scenarios, exercised directly (mirrors how Task 1.7's
# seed_exposure suite tests RiskManager without going through the
# executor).
# ===================================================================
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
        """Two $10 intents in gtm:2026-12 with max_group_usd=15 -> the
        second gets adjusted_size_usd <= 5 (verbatim brief scenario)."""
        rm = self._rm()
        r1 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        rm.add_exposure(r1.adjusted_size_usd, group_key=self.GROUP)

        r2 = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)

        assert r2.approved is True
        assert r2.adjusted_size_usd <= 5.0
        assert r2.adjusted_size_usd == pytest.approx(5.0)

    def test_third_intent_is_rejected_naming_the_group(self):
        """A third $10 intent, after the group is fully at $15, is
        rejected with a reason naming the group (verbatim brief
        scenario)."""
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
        """The three resting orders' $29.64 already exceeds the $15 cap
        by design (they are never cancelled) -- any further gtm:2026-12
        intent is rejected once seeded (verbatim brief scenario)."""
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 29.64})

        result = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)

        assert result.approved is False
        assert self.GROUP in result.reason

    def test_different_group_is_unaffected_by_a_seeded_or_full_group(self):
        """A different print's group is untouched by another group being
        seeded at/over its cap (verbatim brief scenario)."""
        rm = self._rm()
        rm.seed_group_exposure({self.GROUP: 29.64})

        other = rm.check_trade(_edge(question_id="gtm:2026-11:1.0"), proposed_size_usd=10.0,
                               group_key="gtm:2026-11")

        assert other.approved is True
        assert other.adjusted_size_usd == pytest.approx(10.0)

    def test_group_key_empty_is_byte_identical_to_no_group_key_argument(self):
        """Regress note: per-position cap and total-exposure logic must
        be byte-identical when group_key is empty -- calling with an
        explicit "" must match calling check_trade without the argument
        at all (the pre-3.6 call signature)."""
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

        # An intent with NO group key is never capped by ANY group,
        # including one that is already far over its own limit.
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
        """Boundary: nothing committed yet -> the full $15 is available,
        so a $10 intent (under the group cap, but at the $10 position
        cap) is untouched by the group check."""
        rm = self._rm()
        result = rm.check_trade(_edge(), proposed_size_usd=10.0, group_key=self.GROUP)
        assert result.approved is True
        assert result.adjusted_size_usd == pytest.approx(10.0)


class TestSeedGroupExposureIdempotence:
    """Mirrors Task 1.7's seed_exposure idempotence suite exactly, for
    the group-scoped ledger."""

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
        """Regress note: add_exposure's pre-3.6 behavior (bump total,
        nothing else) is preserved when group_key is omitted."""
        rm = RiskManager(bankroll=100.0)
        rm.add_exposure(40.0)
        assert rm._total_exposure == pytest.approx(40.0)
        assert rm._group_exposure == {}


# ===================================================================
# Section B: executor-side group_key derivation (SSOT with reconcile,
# gtm:* scoping) and threading through check_trade/add_exposure.
# ===================================================================
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
        """SINGLE-SOURCE-OF-TRUTH: the executor's derivation must agree
        with `load_open_commitments`'s `by_group` keys for every id shape
        this repo actually produces, or seeded and this-run exposure
        would silently talk past each other."""
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
        # Group headroom after the first $10 order is ~$5; the second
        # order's cost must respect that, not the (higher) per-position cap.
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
        """Ground rule: the cap only blocks NEW placement. Nothing about
        evaluating or rejecting an intent may cancel an order."""
        venue = FakeVenue(read_only=False)
        ex = TradingExecutor(
            venue=venue, bankroll=1000.0, db_path=str(tmp_path / "t.db"), simulate=False,
            risk_limits=RiskLimits(max_position_usd=10.0, max_group_usd=15.0,
                                   max_total_exposure_usd=1000.0),
        )
        ex.execute_intents(
            [self._intent("1.2"), self._intent("1.3"), self._intent("1.4")], mode="auto",
        )
        # Two placed (and possibly clipped), one rejected by the group cap
        # -- never a cancel call anywhere in the venue's order book. (venue
        # order status is domain.models.Order's own string literal, not
        # order_manager.OrderStatus -- compared as a plain string here.)
        assert all(o.status != "cancelled" for o in venue._orders.values())

    def test_execute_intents_evaluates_in_the_given_order_no_edge_sort(self, tmp_path):
        """order-dependent-cap-scaling lesson, pinned structurally: a
        LOW-edge intent placed FIRST in the list claims group headroom
        ahead of a HIGH-edge intent placed second, because execute_intents
        does not reorder by edge. This is the documented limitation, not
        a bug -- if this test starts failing because someone added
        sorting, that is a deliberate, separate, reviewed change."""
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
        # The high-edge intent, evaluated SECOND, only sees the leftover
        # ~$5 of group headroom -- despite its much larger edge.
        assert d_high.executed and d_high.order.cost_usd <= 5.0 + 1e-6


# ===================================================================
# Section C: run-report summary() per-group committed totals.
# ===================================================================
class TestSummaryPerGroupTotals:
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

    def test_acceptance_format_three_strikes_one_correlated_bet(self):
        """Acceptance (Task 3.6 brief, verbatim): the measurement record's
        report shows `gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)`.
        """
        report = ExecutionReport(decisions=[
            self._decision("gtm:2026-12:1.2", 9.86),
            self._decision("gtm:2026-12:1.3", 9.86),
            self._decision("gtm:2026-12:1.4", 9.92),
        ])
        report.trades_executed = 3

        text = report.summary()

        assert "gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)" in text

    def test_ungrouped_decisions_produce_no_group_line(self):
        report = ExecutionReport(decisions=[self._decision("0xabc123", 10.0)])
        report.trades_executed = 1
        text = report.summary()
        assert "correlated bet" not in text

    def test_skipped_decisions_do_not_count_toward_group_totals(self):
        skipped = self._decision("gtm:2026-12:1.2", 10.0)
        skipped.executed = False
        report = ExecutionReport(decisions=[
            skipped,
            self._decision("gtm:2026-12:1.3", 5.0),
        ])
        report.trades_executed = 1
        text = report.summary()
        assert "gtm:2026-12 = $5.00 (1 orders, 1 correlated bet)" in text

    def test_two_distinct_groups_each_get_their_own_line(self):
        report = ExecutionReport(decisions=[
            self._decision("gtm:2026-12:1.2", 10.0),
            self._decision("gtm:2026-11:1.0", 7.5),
        ])
        report.trades_executed = 2
        text = report.summary()
        assert "gtm:2026-12 = $10.00 (1 orders, 1 correlated bet)" in text
        assert "gtm:2026-11 = $7.50 (1 orders, 1 correlated bet)" in text


# ===================================================================
# Section D: docstring-carried decisions (known limitation, verbatim).
# ===================================================================
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


# ===================================================================
# Section E: scripts/run_ibkr_paper.py wiring (structural pin, matching
# the pattern the Task 1.7 TOCTOU test already established for
# seed_exposure -- driving main() needs a live Gateway, out of reach for
# a unit test, so this pins the invariant in source order instead).
# ===================================================================
def test_run_ibkr_paper_seeds_group_exposure_next_to_total_exposure_inside_the_lock():
    import scripts.run_ibkr_paper as mod

    source = inspect.getsource(mod.main)
    lock_pos = source.index("with acquire_db_lock(")
    seed_total_pos = source.index("seed_exposure(")
    seed_group_pos = source.index("seed_group_exposure(")

    assert seed_total_pos > lock_pos
    assert seed_group_pos > lock_pos
    assert "commitments[\"by_group\"]" in source or "commitments['by_group']" in source
