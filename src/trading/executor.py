"""Trading executor — ties research results to trade execution.

Orchestrates the full trade flow:
1. Takes ResearchResult from the analysis pipeline
2. Runs risk checks (RiskManager)
3. Creates orders (edge_to_order)
4. Presents trades for human approval (optional)
5. Places orders via OrderManager
6. Records positions via PositionTracker

Supports three modes:
- dry_run: Simulate everything locally (default, safe)
- approve: Show each trade, require human confirmation
- auto: Execute automatically (only for live trading after thorough testing)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.agent.researcher import ResearchResult
from src.config import settings
from src.trading.order_manager import OrderManager, TradeOrder, edge_to_order
from src.trading.position_tracker import PositionTracker
from src.trading.risk_manager import RiskCheckResult, RiskLimits, RiskManager
from src.trading.wallet import PolymarketWallet

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    """Result of evaluating one ResearchResult for trading."""

    question_id: str
    question_title: str
    edge_pct: float
    direction: str
    risk_check: RiskCheckResult
    order: TradeOrder | None = None
    executed: bool = False
    reason: str = ""


@dataclass
class ExecutionReport:
    """Summary of a batch trading run."""

    timestamp: datetime = field(default_factory=datetime.now)
    total_analyzed: int = 0
    trades_proposed: int = 0
    trades_approved: int = 0
    trades_executed: int = 0
    trades_rejected: int = 0
    total_cost_usd: float = 0.0
    decisions: list[TradeDecision] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"=== Execution Report ({self.timestamp:%Y-%m-%d %H:%M}) ===",
            f"Analyzed: {self.total_analyzed} questions",
            f"Proposed: {self.trades_proposed} trades",
            f"Approved: {self.trades_approved}",
            f"Executed: {self.trades_executed}",
            f"Rejected: {self.trades_rejected}",
            f"Total cost: ${self.total_cost_usd:.2f}",
        ]

        if self.decisions:
            lines.append("")
            lines.append("Decisions:")
            for d in self.decisions:
                status = "EXECUTED" if d.executed else "SKIPPED"
                lines.append(
                    f"  [{status}] {d.question_title[:40]}: "
                    f"{d.direction} edge={d.edge_pct:+.1%} | {d.reason}"
                )

        return "\n".join(lines)


class TradingExecutor:
    """Main execution engine: research results → trades.

    Usage:
        executor = TradingExecutor(dry_run=True)
        executor.connect()
        report = executor.execute(results, mode="approve")
        print(report.summary())
    """

    def __init__(
        self,
        dry_run: bool = True,
        private_key: str = "",
        bankroll: float = 1000.0,
        risk_limits: RiskLimits | None = None,
        db_path: str | None = None,
    ):
        self._dry_run = dry_run
        self._db_path = db_path or settings.database_path

        # Initialize components
        self._wallet = PolymarketWallet(
            private_key=private_key,
            dry_run=dry_run,
        )
        self._risk_manager = RiskManager(
            limits=risk_limits,
            bankroll=bankroll,
        )
        self._order_manager = OrderManager(wallet=self._wallet)
        self._position_tracker = PositionTracker(db_path=self._db_path)

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    @property
    def wallet(self) -> PolymarketWallet:
        return self._wallet

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk_manager

    def connect(self) -> bool:
        """Connect wallet. Must call before executing trades."""
        success = self._wallet.connect()
        if success:
            mode = "DRY-RUN" if self._dry_run else "LIVE"
            logger.info(f"Trading executor ready ({mode} mode)")
        else:
            logger.error("Failed to connect wallet")
        return success

    def execute(
        self,
        results: list[ResearchResult],
        mode: str = "approve",
    ) -> ExecutionReport:
        """Process research results and execute trades.

        Args:
            results: List of ResearchResult from the analysis pipeline
            mode: "dry_run" (simulate only), "approve" (human confirms each),
                  "auto" (execute all approved by risk manager)

        Returns:
            ExecutionReport with all decisions and outcomes.
        """
        report = ExecutionReport(total_analyzed=len(results))

        for result in results:
            decision = self._evaluate(result)
            report.decisions.append(decision)

            if not decision.risk_check.approved:
                report.trades_rejected += 1
                continue

            report.trades_proposed += 1

            if mode == "approve":
                approved = self._prompt_approval(decision)
            elif mode == "auto":
                approved = True
            else:  # dry_run — always approve in simulation
                approved = True

            if not approved:
                decision.reason = "User rejected"
                report.trades_rejected += 1
                continue

            report.trades_approved += 1

            # Place the order
            success = self._place(decision)
            if success:
                decision.executed = True
                report.trades_executed += 1
                report.total_cost_usd += decision.order.cost_usd
            else:
                report.trades_rejected += 1

        logger.info(
            f"Execution complete: {report.trades_executed}/{report.trades_proposed} "
            f"trades placed, ${report.total_cost_usd:.2f} total"
        )
        return report

    def _evaluate(self, result: ResearchResult) -> TradeDecision:
        """Evaluate a single research result for trading."""
        edge = result.edge

        # Skip if no edge detected
        if not result.has_edge:
            return TradeDecision(
                question_id=result.question.id,
                question_title=result.question.title,
                edge_pct=edge.edge_pct,
                direction=edge.direction,
                risk_check=RiskCheckResult(
                    approved=False,
                    reason="No edge detected",
                ),
                reason="No edge detected",
            )

        # Calculate days to deadline
        days_to_deadline = (result.question.deadline - datetime.now()).days

        # Proposed size from Kelly
        proposed_size = edge.kelly_fraction * self._risk_manager.bankroll

        # Run risk checks
        risk_result = self._risk_manager.check_trade(
            edge=edge,
            proposed_size_usd=proposed_size,
            days_to_deadline=max(days_to_deadline, 0),
        )

        decision = TradeDecision(
            question_id=result.question.id,
            question_title=result.question.title,
            edge_pct=edge.edge_pct,
            direction=edge.direction,
            risk_check=risk_result,
            reason=risk_result.reason,
        )

        # If approved, create the order
        if risk_result.approved:
            # Get token IDs from market data if available
            yes_token_id = getattr(result.question.market, "yes_token_id", "")
            no_token_id = getattr(result.question.market, "no_token_id", "")

            order = edge_to_order(
                edge=edge,
                size_usd=risk_result.adjusted_size_usd,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
            decision.order = order

        return decision

    def _prompt_approval(self, decision: TradeDecision) -> bool:
        """Show trade details and ask for human confirmation."""
        order = decision.order
        if not order:
            return False

        print("\n" + "=" * 60)
        print("TRADE PROPOSAL")
        print("=" * 60)
        print(f"  Question: {decision.question_title}")
        print(f"  Direction: {decision.direction}")
        print(f"  Edge: {decision.edge_pct:+.1%}")
        print(f"  Action: BUY {order.outcome} @ ${order.price:.4f}")
        print(f"  Size: {order.size:.2f} tokens")
        print(f"  Cost: ${order.cost_usd:.2f}")
        print(f"  Reason: {order.reason}")
        print()

        # Show risk check details
        rc = decision.risk_check
        print(f"  Risk checks passed: {len(rc.checks_passed)}")
        for check in rc.checks_passed:
            print(f"    ✓ {check}")
        if rc.checks_failed:
            print(f"  Risk checks failed: {len(rc.checks_failed)}")
            for check in rc.checks_failed:
                print(f"    ✗ {check}")

        mode_label = "[DRY-RUN]" if self._dry_run else "[LIVE]"
        print(f"\n  Mode: {mode_label}")
        print("=" * 60)

        try:
            response = input("  Approve this trade? [y/N]: ").strip().lower()
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return False

    def _place(self, decision: TradeDecision) -> bool:
        """Place an approved order and record the trade."""
        order = decision.order
        if not order:
            return False

        success = self._order_manager.place_order(order)

        if success:
            # Record in position tracker
            self._position_tracker.record_trade(order)

            # Update risk manager exposure
            self._risk_manager.add_exposure(order.cost_usd)

            logger.info(
                f"Trade placed: {order.outcome} {decision.question_title[:30]} "
                f"@ ${order.price:.4f} x {order.size:.1f} = ${order.cost_usd:.2f}"
            )
        else:
            decision.reason = f"Order rejected: {order.reason}"
            logger.warning(f"Trade failed: {decision.question_title[:30]} — {order.reason}")

        return success

    def get_portfolio(self) -> dict:
        """Get current portfolio state."""
        return {
            "wallet": {
                "dry_run": self._dry_run,
                "connected": self._wallet.is_connected,
                "balance": self._wallet.get_balance().__dict__,
            },
            "risk": {
                "bankroll": self._risk_manager.bankroll,
            },
            "positions": self._position_tracker.get_portfolio_summary(),
            "orders": self._order_manager.get_trade_summary(),
        }
