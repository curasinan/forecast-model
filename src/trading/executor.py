"""Trading executor -- ties edge signals to execution through a VenueAdapter."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config import settings
from src.domain.ids import MarketId
from src.domain.models import Market
from src.math.expected_value import expected_value, expected_value_no
from src.math.kelly import kelly_for_no, kelly_fraction
from src.models.prediction import EdgeSignal
from src.trading.order_manager import OrderManager, TradeOrder, edge_to_order
from src.trading.position_tracker import PositionTracker
from src.trading.risk_manager import RiskCheckResult, RiskLimits, RiskManager
from src.venues.base import VenueAdapter

logger = logging.getLogger(__name__)


def _group_key(question_id: str) -> str:
    """The correlated-print group for a question_id (H5 cap)."""
    if not question_id.startswith("gtm:"):
        return ""
    return question_id.rsplit(":", 1)[0]


@dataclass
class TradeIntent:
    """One candidate trade, venue-neutral: an edge on a market."""

    edge: EdgeSignal
    market: Market
    title: str = ""
    days_to_resolution: int | None = None  # overrides market.scheduled_resolution_time
    liquidity_usd: float = 0.0
    no_price: float | None = None  # NO-leg quote when the venue quotes it separately


@dataclass
class TradeDecision:
    """Result of evaluating one intent."""

    question_id: str
    question_title: str
    edge_pct: float
    direction: str
    risk_check: RiskCheckResult
    order: TradeOrder | None = None
    executed: bool = False
    reason: str = ""
    net_expected_value: float | None = None
    net_kelly_fraction: float | None = None
    holding_days: float | None = None


@dataclass
class ExecutionReport:
    """Summary of a batch trading run."""

    timestamp: datetime = field(default_factory=datetime.now)
    venue: str = ""
    simulated: bool = True
    total_analyzed: int = 0
    trades_proposed: int = 0
    trades_approved: int = 0
    trades_executed: int = 0
    trades_rejected: int = 0
    total_cost_usd: float = 0.0
    decisions: list[TradeDecision] = field(default_factory=list)

    def summary(self) -> str:
        """This run's own analyze/propose/approve/execute/reject counts and per-decision lines."""
        mode = "SIMULATED" if self.simulated else f"VENUE:{self.venue}"
        lines = [
            f"=== Execution Report ({self.timestamp:%Y-%m-%d %H:%M}) [{mode}] ===",
            f"  Analyzed: {self.total_analyzed}",
            f"  Proposed: {self.trades_proposed}",
            f"  Approved: {self.trades_approved}",
            f"  Executed: {self.trades_executed}",
            f"  Rejected: {self.trades_rejected}",
            f"  Total cost: ${self.total_cost_usd:.2f}",
        ]
        if self.decisions:
            lines.append("")
            lines.append("  Decisions:")
            for d in self.decisions:
                status = "EXECUTED" if d.executed else "SKIPPED"
                lines.append(
                    f"    [{status}] {d.question_title[:42]:<42} {d.direction:<16} "
                    f"edge={d.edge_pct:+.1%} {d.reason[:60]}"
                )
        return "\n".join(lines)

    def group_order_counts(self) -> dict[str, int]:
        """This run's OWN order count per correlated group (H5)."""
        counts: dict[str, int] = {}
        for d in self.decisions:
            if not d.executed or d.order is None:
                continue
            key = _group_key(d.question_id)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts


def render_group_commitments(group_exposure: dict[str, float], group_counts: dict[str, int]) -> str:
    """Render the per-correlated-group COMMITTED BOOK block for a run's printed report."""
    if not group_exposure:
        return ""
    lines = ["Correlated groups (committed book per print -- DB-seeded + this run):"]
    for key in sorted(group_exposure):
        total = group_exposure[key]
        count = group_counts.get(key, 0)
        noun = "order" if count == 1 else "orders"
        lines.append(f"  {key} = ${total:.2f} ({count} {noun}, 1 correlated bet)")
    return "\n".join(lines)


def _days_until(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    now = datetime.now(timezone.utc) if ts.tzinfo else datetime.now()
    return max(0, (ts - now).days)


class TradingExecutor:
    """Orchestrates intent -> risk -> order -> venue -> ledger."""

    def __init__(
        self,
        venue: VenueAdapter,
        bankroll: float = 1000.0,
        risk_limits: RiskLimits | None = None,
        db_path: str | None = None,
        simulate: bool | None = None,
        approval_input: Callable[[str], str] = input,
    ):
        self._venue = venue
        self._db_path = db_path or settings.database_path
        self._risk_manager = RiskManager(limits=risk_limits, bankroll=bankroll)
        self._order_manager = OrderManager(venue, simulate=simulate, sim_bankroll=bankroll)
        self._position_tracker = PositionTracker(db_path=self._db_path)
        self._approval_input = approval_input

    # -- properties --------------------------------------------------------
    @property
    def venue(self) -> VenueAdapter:
        return self._venue

    @property
    def is_simulated(self) -> bool:
        return self._order_manager.is_simulated

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk_manager

    @property
    def order_manager(self) -> OrderManager:
        return self._order_manager

    @property
    def position_tracker(self) -> PositionTracker:
        return self._position_tracker

    def connect(self) -> bool:
        """Connect the venue (idempotent for adapters that are already up)."""
        try:
            self._venue.connect()
        except Exception as exc:  # noqa: BLE001 -- surface, don't hide
            logger.error("venue connect failed: %s", exc)
            return False
        logger.warning(
            "executor ready: venue=%s simulated=%s read_only=%s",
            self._venue.name, self.is_simulated, self._venue.read_only,
        )
        return True

    # -- main entry points -------------------------------------------------
    def execute(self, results: Iterable, mode: str = "approve") -> ExecutionReport:
        """Back-compat entry for the research pipeline (ResearchResult objects)."""
        intents: list[TradeIntent] = []
        for r in results:
            if not getattr(r, "has_edge", False):
                continue
            q = r.question
            md = q.market
            market = Market(
                market_id=MarketId.polymarket(md.market_id or q.id),
                symbol=md.slug or "",
                title=q.title,
                venue_meta={"slug": md.slug},
            )
            intents.append(TradeIntent(
                edge=r.edge,
                market=market,
                title=q.title,
                days_to_resolution=max(0, (q.deadline - datetime.now()).days),
                liquidity_usd=float(md.liquidity or 0.0),
            ))
        return self.execute_intents(intents, mode=mode)

    def execute_intents(self, intents: Iterable[TradeIntent], mode: str = "approve") -> ExecutionReport:
        """Evaluate, and (mode-permitting) place, one intent at a time.

        Ordering semantics (H5 group cap, Task 3.6; order-dependent-cap-
        scaling lesson): intents are evaluated STRICTLY in the order
        `intents` yields them -- this method does not sort by edge, EV, or
        anything else. That matters because `RiskManager`'s per-group cap
        (`check_trade(group_key=...)`) consumes group headroom greedily as
        each intent is accepted, so within one correlated group, headroom
        goes to whichever intent is evaluated FIRST, not whichever has the
        best edge. This executor does not sort intents itself and does not
        add sorting here (scope discipline for Task 3.6 -- a change to
        evaluation order is a separate, reviewed decision). In production,
        `scripts/run_ibkr_paper.py::load_intents` already sorts its rows
        by descending `abs(edge)` before building `TradeIntent`s, so
        headroom is in practice consumed highest-edge-first there; a
        caller that hands intents in a different order (e.g. most tests)
        gets first-come, first-served instead.
        """
        report = ExecutionReport(venue=self._venue.name, simulated=self.is_simulated)
        for intent in intents:
            report.total_analyzed += 1
            decision = self._evaluate(intent)
            report.decisions.append(decision)

            if not decision.risk_check.approved:
                report.trades_rejected += 1
                continue
            report.trades_proposed += 1

            if mode == "approve":
                approved = self._prompt_approval(decision)
            else:
                approved = True
            if not approved:
                decision.reason = "User rejected"
                report.trades_rejected += 1
                continue
            report.trades_approved += 1

            if self._place(decision):
                decision.executed = True
                report.trades_executed += 1
                report.total_cost_usd += decision.order.cost_usd if decision.order else 0.0
            else:
                report.trades_rejected += 1

        logger.info(
            "execution complete: %d/%d placed, $%.2f total (%s)",
            report.trades_executed, report.trades_proposed, report.total_cost_usd,
            "simulated" if report.simulated else self._venue.name,
        )
        return report

    # -- evaluation --------------------------------------------------------
    def _evaluate(self, intent: TradeIntent) -> TradeDecision:
        edge = intent.edge
        title = intent.title or intent.market.title or edge.question_id

        min_edge_pct = self._risk_manager.limits.min_edge_pct
        if not (abs(edge.edge_pct) > min_edge_pct and edge.expected_value > 0):
            return TradeDecision(
                question_id=edge.question_id, question_title=title,
                edge_pct=edge.edge_pct, direction=edge.direction,
                risk_check=RiskCheckResult(approved=False, reason="No edge detected"),
                reason="No edge detected",
            )

        days = intent.days_to_resolution
        if days is None:
            days = _days_until(intent.market.scheduled_resolution_time)
        if days is None:
            days = _days_until(intent.market.expiry)
        if days is None:
            days = 30
        holding_days = float(days)

        # Re-price with the venue's economics over the holding period.
        fee_model = self._venue.fee_model()
        carry_model = self._venue.carry_model()
        p, yes_price = edge.model_prob, edge.market_prob
        kwargs = {"fee_model": fee_model, "carry_model": carry_model, "holding_days": holding_days}
        if edge.edge_pct > 0:
            net_ev = expected_value(p, yes_price, **kwargs)
            kelly = kelly_fraction(p, yes_price, **kwargs)
        else:
            net_ev = expected_value_no(p, yes_price, **kwargs)
            kelly = kelly_for_no(p, yes_price, **kwargs)
        kelly *= 0.5
        if edge.vpin_flag:
            kelly *= 0.5
        kelly *= edge.confidence
        priced = edge.model_copy(update={"expected_value": net_ev, "kelly_fraction": kelly})

        proposed_size = priced.kelly_fraction * self._risk_manager.bankroll
        risk_result = self._risk_manager.check_trade(
            edge=priced,
            proposed_size_usd=proposed_size,
            market_liquidity=intent.liquidity_usd,
            days_to_deadline=int(days),
            group_key=_group_key(edge.question_id),
        )
        decision = TradeDecision(
            question_id=edge.question_id, question_title=title,
            edge_pct=edge.edge_pct, direction=edge.direction,
            risk_check=risk_result, reason=risk_result.reason,
            net_expected_value=net_ev, net_kelly_fraction=kelly, holding_days=holding_days,
        )
        if risk_result.approved:
            decision.order = edge_to_order(
                edge=priced,
                size_usd=risk_result.adjusted_size_usd,
                market=intent.market,
                fee_model=fee_model,
                no_price=intent.no_price,
            )
        return decision

    def _prompt_approval(self, decision: TradeDecision) -> bool:
        order = decision.order
        if not order:
            return False
        print("\n" + "=" * 60)
        print("TRADE PROPOSAL")
        print("=" * 60)
        print(f"  Market:    {decision.question_title}")
        print(f"  Direction: {decision.direction}")
        print(f"  Edge:      {decision.edge_pct:+.1%}  net EV/contract: "
              f"{decision.net_expected_value:+.4f}  holding: {decision.holding_days:.0f}d")
        print(f"  Action:    BUY {order.outcome} leg {order.market_id} @ ${order.price:.4f}")
        print(f"  Size:      {order.size:g} contracts  Cost: ${order.cost_usd:.2f}")
        print(f"  Reason:    {order.reason}")
        rc = decision.risk_check
        for check in rc.checks_passed:
            print(f"    + {check}")
        for check in rc.checks_failed:
            print(f"    - {check}")
        print(f"\n  Mode: {'SIMULATED' if self.is_simulated else 'VENUE ' + self._venue.name.upper()}")
        print("=" * 60)
        try:
            return self._approval_input("  Approve this trade? [y/N]: ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return False

    def _place(self, decision: TradeDecision) -> bool:
        order = decision.order
        if not order:
            return False
        ok = self._order_manager.place_order(order)
        if ok:
            self._position_tracker.record_trade(order)
            self._risk_manager.add_exposure(order.cost_usd, group_key=_group_key(decision.question_id))
            logger.info(
                "trade %s: %s %s @ %.4f x %g = $%.2f (%s)",
                order.status.value, order.outcome, decision.question_title[:30],
                order.price, order.size, order.cost_usd, self._venue.name,
            )
        else:
            decision.reason = f"Order rejected: {order.reason}"
            logger.warning("trade failed: %s -- %s", decision.question_title[:30], order.reason)
        return ok

    def sync(self) -> int:
        """Pull fill/cancel status from the venue for placed orders; record fills."""
        updated = self._order_manager.sync_with_venue()
        for order in self._order_manager.filled_orders:
            self._position_tracker.record_trade(order)  # INSERT OR REPLACE keeps it idempotent
        for order in self._order_manager.backfill_perm_ids():
            self._position_tracker.backfill_perm_id(order.order_id, order.perm_id)
        return updated

    def get_portfolio(self) -> dict:
        balances = positions = None
        if not self.is_simulated:
            try:
                balances = [b.model_dump() for b in self._venue.get_balances()]
                positions = [p.model_dump() for p in self._venue.get_positions()]
            except Exception as exc:  # noqa: BLE001
                logger.warning("venue portfolio query failed: %s", exc)
        return {
            "venue": self._venue.name,
            "simulated": self.is_simulated,
            "simulated_balance": self._order_manager.simulated_balance,
            "venue_balances": balances,
            "venue_positions": positions,
            "risk": {"bankroll": self._risk_manager.bankroll},
            "positions": self._position_tracker.get_portfolio_summary(),
            "orders": self._order_manager.get_trade_summary(),
        }
