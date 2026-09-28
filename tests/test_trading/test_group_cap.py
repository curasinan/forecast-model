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

Fix round 1 (controller ruling R16, finding I-1): the run report's
per-group block must render the COMMITTED BOOK -- `RiskManager.
group_exposure()` (DB-seeded via `seed_group_exposure`, MAX'd, plus
this run's own `add_exposure(group_key=...)` calls) and a matching
order COUNT (DB-seeded `load_open_commitments(...)["by_group_counts"]`
plus this run's own `ExecutionReport.group_order_counts()`) -- never a
sum over `self.decisions` alone, which can never show a commitment
`drop_already_committed` correctly kept out of a run's decisions
altogether (the three resting gtm:2026-12 orders). See
`TestRenderGroupCommitments`, `TestSummaryDoesNotRenderGroupsFromDecisionsAlone`,
and `TestGroupCommitmentsBehaviorAgainstADB` below.
"""

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


def _make_book(path) -> None:
    """Create a v2-shaped paper book (PositionTracker owns the DDL) --
    same helper pattern as test_placement_idempotence.py."""
    PositionTracker(db_path=str(path))


def _insert_placed_row(path, *, order_id: str, question_id: str, market_id: str,
                       size: float, price: float) -> None:
    """A resting `status='placed'` trade_log row -- shaped like a real
    GTM order (size/price, not a pre-computed cost_usd) so the DB-driven
    behavior tests below match production data, not a convenient fixture.
    """
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
    """Exactly the merge scripts/run_ibkr_paper.py performs (ruling R16
    item 6's threading): DB-seeded counts plus this run's own count,
    added per group by the one layer holding both halves."""
    counts = dict(commitments["by_group_counts"])
    for key, n in report.group_order_counts().items():
        counts[key] = counts.get(key, 0) + n
    return counts


# ===================================================================
# Section C: render_group_commitments -- the pure render function (H5,
# Task 3.6; controller ruling R16 fix for finding I-1). Format-only
# tests: these exercise ONLY string rendering from already-correct,
# already-merged input dicts -- the "is this data actually correct"
# question belongs to Section C2/C3 below, not here.
# ===================================================================
class TestRenderGroupCommitments:
    def test_acceptance_format_three_strikes_one_correlated_bet(self):
        """Acceptance (Task 3.6 brief, verbatim): `gtm:2026-12 = $29.64
        (3 orders, 1 correlated bet)`."""
        text = render_group_commitments({"gtm:2026-12": 29.64}, {"gtm:2026-12": 3})
        assert "gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)" in text

    def test_singular_order_is_not_pluralized(self):
        """Ruling item 4's in-passing pluralization fix: "1 orders" ->
        "1 order"."""
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
        """group_counts is caller-merged and need not cover every
        group_exposure key -- a defensive `.get(key, 0)`, not a
        KeyError."""
        text = render_group_commitments({"gtm:2026-12": 5.0}, {})
        assert "gtm:2026-12 = $5.00 (0 orders, 1 correlated bet)" in text

    def test_header_states_it_is_the_committed_book_seeded_plus_this_run(self):
        """The corrected header wording (ruling item 3) -- must say what
        it shows, not "this run" alone (the old, wrong wording)."""
        text = render_group_commitments({"gtm:2026-12": 1.0}, {"gtm:2026-12": 1})
        assert "committed book" in text.lower()
        assert "seed" in text.lower()
        assert "this run" in text.lower()


# ===================================================================
# Section C2: summary() must NOT render group commitments from
# self.decisions alone (the actual fix for finding I-1) -- and
# group_order_counts() (this run's own contribution, still correct)
# is exposed for a caller to merge with the DB-seeded count.
# ===================================================================
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
        """Even with three executed decisions that would have summed to
        the acceptance figure under the OLD (wrong) design, summary()
        prints no group line at all -- it has no ledger to read, and
        must not pretend otherwise."""
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


# ===================================================================
# Section C3: RiskManager.group_exposure() -- the read-only ledger
# snapshot the render path is now built on (ruling item 1).
# ===================================================================
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


# ===================================================================
# Section C4: the ruling's required BEHAVIOR test (item 5) -- a temp DB
# shaped like the real book, run through the actual seeding path, no
# venue, no Gateway. This is what proves finding I-1 is actually fixed:
# the acceptance line must come out of DATA the three resting orders
# would really produce, not a hand-built decisions list.
# ===================================================================
class TestGroupCommitmentsBehaviorAgainstADB:
    def test_the_real_three_strike_book_renders_the_acceptance_line(self, tmp_path):
        """29@0.34 / 41@0.24 / 71@0.14 -- the actual resting book
        (docs/plans/2026-08-25-audit-remediation.md Task 0.2) -- must
        render EXACTLY the brief's acceptance line, driven end-to-end
        through load_open_commitments + seed_group_exposure +
        render_group_commitments. This run places nothing new in the
        group (mirrors production, where drop_already_committed drops
        all three intents before the executor ever sees them) -- which
        is exactly the scenario the OLD, decisions-only design could
        never render at all (finding I-1)."""
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
        """The finding's own misleading example: a group seeded at $8
        plus a $7 placement THIS run must render $15.00 committed across
        2 orders -- never $7.00, which is what the OLD (wrong) design
        would have shown by summing only this run's own decisions."""
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
        """The full production wiring (ruling item 6), using FakeVenue
        instead of a real Gateway: seed from the DB, run one new intent
        through TradingExecutor.execute_intents, merge
        report.group_order_counts() with the DB-seeded count exactly as
        scripts/run_ibkr_paper.py now does, and render."""
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


def test_run_ibkr_paper_renders_the_committed_book_from_the_ledger_not_the_report_alone():
    """Ruling R16 item 6's threading requirement, pinned structurally the
    same way (a live Gateway is out of reach for a unit test): the
    printed group block must be built from `risk_manager.group_exposure()`
    (the ledger) and `commitments["by_group_counts"]` (the DB-seeded
    count) merged with the report's OWN count -- not from `report.
    summary()` alone, which cannot see the seeded book (finding I-1)."""
    import scripts.run_ibkr_paper as mod

    source = inspect.getsource(mod.main)
    lock_pos = source.index("with acquire_db_lock(")
    report_pos = source.index("executor.execute_intents(")
    render_pos = source.index("render_group_commitments(")
    group_exposure_pos = source.index("executor.risk_manager.group_exposure()")
    by_group_counts_pos = source.index("commitments[\"by_group_counts\"]")

    for marker_pos in (report_pos, render_pos, group_exposure_pos, by_group_counts_pos):
        assert marker_pos > lock_pos
    # The block is rendered AFTER the executor has run (it needs this
    # run's own count/exposure on top of the seed), not before.
    assert render_pos > report_pos
    assert group_exposure_pos > report_pos
