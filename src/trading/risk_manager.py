"""Risk manager — pre-trade safety checks.

Every trade must pass ALL risk checks before execution:
1. Edge threshold: minimum edge % to trade
2. Position limits: max $ per market, max total exposure
3. Daily loss limit: stop trading if daily loss exceeds threshold
4. VPIN filter: block trades when informed trading is detected
5. Confidence floor: require minimum model confidence
6. Kelly cap: never exceed half-Kelly sizing
7. Liquidity check: don't trade illiquid markets

This is the safety gate between "we detected an edge" and "we place a trade."
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.models.prediction import EdgeSignal

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Configurable risk parameters.

    Known limitation (H5 correlated-group concentration cap, Task 3.6;
    operator-approved 2026-08-25): `max_group_usd` caps committed notional
    per correlated PRINT, where the group key is `question_id` minus its
    trailing ':<strike>' segment (e.g. "gtm:2026-12:1.3" -> "gtm:2026-12"
    -- `src.trading.reconcile._group_key` is the single source of truth
    for that expression; any caller deriving a group key must match it
    exactly). That key is per (symbol-lane, target month) -- CROSS-SYMBOL
    correlation is NOT grouped. GT (annual), GTM (monthly), and GTTA/GTTM
    (pre-industrial-threshold) all settle off the same underlying NOAA
    print for a given month/year -- the same real-world outcome -- yet
    each would get its own group today, because the group key includes
    the symbol-lane prefix. A book trading more than one of these lanes
    on the same underlying print is more correlated than this cap can
    see. Revisit the grouping key before opening a second symbol lane.
    """

    # Edge requirements
    min_edge_pct: float = 0.05  # 5% minimum edge to trade
    min_confidence: float = 0.40  # Minimum ensemble confidence
    min_ev: float = 0.02  # Minimum expected value ($0.02)

    # Position sizing
    max_position_usd: float = 50.0  # Max $ per single market
    max_total_exposure_usd: float = 200.0  # Max total across all positions
    max_group_usd: float = 15.0  # Max $ committed per correlated print (H5, Task 3.6)
    kelly_cap: float = 0.25  # Never bet more than 25% of bankroll
    max_kelly_fraction: float = 0.50  # Cap raw Kelly at 50%

    # Loss limits
    max_daily_loss_usd: float = 50.0  # Stop trading after this daily loss
    max_drawdown_pct: float = 0.20  # Stop after 20% drawdown from peak

    # Market quality
    min_liquidity_usd: float = 100.0  # Minimum market liquidity
    block_high_vpin: bool = True  # Block trades when VPIN is elevated

    # Timing
    min_days_to_deadline: int = 3  # Don't trade markets expiring soon


@dataclass
class RiskCheckResult:
    """Result of a risk check on a proposed trade."""

    approved: bool
    reason: str
    adjusted_size_usd: float = 0.0  # Risk-adjusted position size
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)


class RiskManager:
    """Pre-trade risk assessment.

    Every trade proposal goes through check_trade() before execution.
    The risk manager can approve, reject, or adjust position size.
    """

    def __init__(self, limits: RiskLimits | None = None, bankroll: float = 1000.0):
        self.limits = limits or RiskLimits()
        self._bankroll = bankroll
        self._daily_pnl = 0.0
        self._daily_reset_date = datetime.now().date()
        self._total_exposure = 0.0
        self._group_exposure: dict[str, float] = {}
        self._peak_bankroll = bankroll

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @bankroll.setter
    def bankroll(self, value: float):
        self._bankroll = value
        self._peak_bankroll = max(self._peak_bankroll, value)

    def check_trade(
        self,
        edge: EdgeSignal,
        proposed_size_usd: float,
        market_liquidity: float = 0.0,
        days_to_deadline: int = 30,
        group_key: str = "",
    ) -> RiskCheckResult:
        """Run all risk checks on a proposed trade.

        Args:
            edge: The edge signal from our model
            proposed_size_usd: How much we want to trade ($)
            market_liquidity: Current market liquidity ($)
            days_to_deadline: Days until market resolution
            group_key: The correlated-print group this trade belongs to
                (H5 concentration cap, Task 3.6), e.g. "gtm:2026-12".
                Derive it with the exact same expression as
                `src.trading.reconcile._group_key` -- that function is the
                single source of truth this method's group ledger must
                agree with. "" (the default, and what every non-`gtm:*`
                question_id gets) means "no group": the group-cap check
                below is skipped ENTIRELY, so every existing check and the
                adjusted size stay byte-identical to before this cap
                existed (regress note, Task 3.6 brief).

                Ordering semantics (order-dependent-cap-scaling lesson):
                this method has no notion of "arrival order" of its own --
                it just reads/clips against whatever `self._group_exposure`
                holds right now. The order in which headroom is actually
                consumed is entirely up to the CALLER's evaluation order.
                `TradingExecutor.execute_intents` evaluates intents
                strictly in the order it is given them (it does not sort
                by edge or anything else); see its docstring for how that
                order is (and is not) controlled upstream. A cap evaluated
                in arrival order can let a low-edge intent claim headroom
                a higher-edge intent evaluated later would have wanted --
                that tradeoff is accepted here, not silently hidden.

        Returns:
            RiskCheckResult with approval status and adjusted size.
        """
        self._maybe_reset_daily()

        passed = []
        failed = []
        size = proposed_size_usd

        # 1. Edge threshold
        if abs(edge.edge_pct) >= self.limits.min_edge_pct:
            passed.append(f"edge={edge.edge_pct:+.1%} >= {self.limits.min_edge_pct:.0%}")
        else:
            failed.append(f"edge={edge.edge_pct:+.1%} < {self.limits.min_edge_pct:.0%} minimum")

        # 2. EV threshold
        if edge.expected_value >= self.limits.min_ev:
            passed.append(f"EV=${edge.expected_value:+.3f} >= ${self.limits.min_ev:.2f}")
        else:
            failed.append(f"EV=${edge.expected_value:+.3f} < ${self.limits.min_ev:.2f} minimum")

        # 3. Confidence floor
        if edge.confidence >= self.limits.min_confidence:
            passed.append(f"confidence={edge.confidence:.0%} >= {self.limits.min_confidence:.0%}")
        else:
            failed.append(
                f"confidence={edge.confidence:.0%} < {self.limits.min_confidence:.0%} minimum"
            )

        # 4. VPIN filter
        if edge.vpin_flag and self.limits.block_high_vpin:
            failed.append("VPIN elevated — informed trading detected")
        else:
            passed.append("VPIN OK" if not edge.vpin_flag else "VPIN elevated but override on")

        # 5. Position size cap
        size = min(size, self.limits.max_position_usd)
        passed.append(f"position capped at ${self.limits.max_position_usd:.0f}")

        # 6. Kelly cap
        kelly = min(edge.kelly_fraction, self.limits.max_kelly_fraction)
        kelly_size = kelly * self._bankroll
        size = min(size, kelly_size)
        passed.append(f"Kelly={kelly:.1%} -> ${kelly_size:.2f}")

        # 7. Total exposure
        remaining_capacity = self.limits.max_total_exposure_usd - self._total_exposure
        if remaining_capacity <= 0:
            failed.append(
                f"total exposure ${self._total_exposure:.0f} >= "
                f"${self.limits.max_total_exposure_usd:.0f} limit"
            )
        else:
            size = min(size, remaining_capacity)
            passed.append(f"exposure headroom: ${remaining_capacity:.0f}")

        # 7a. Group concentration cap (H5, Task 3.6). Skipped ENTIRELY when
        # group_key is "" -- that is the regress note: every pre-existing
        # caller (group_key defaults to "") sees identical checks_passed/
        # checks_failed and adjusted_size_usd to before this cap existed.
        #
        # `self._group_exposure[group_key]` is the group's ledger: it
        # already combines whatever `seed_group_exposure` restored from the
        # DB's open book (resting/committed orders, possibly from a prior
        # process) with whatever THIS process has itself added via
        # `add_exposure(..., group_key=...)` for intents already accepted
        # earlier in this same run. Counting both together -- not just
        # this run's own adds -- is the missing-position-dedup fix applied
        # to a group cap: a ledger that only counted this run's adds would
        # let a second run's intent ride a group a PRIOR run already
        # filled up to $29.64 on.
        if group_key:
            group_committed = self._group_exposure.get(group_key, 0.0)
            group_headroom = self.limits.max_group_usd - group_committed
            if group_headroom <= 0:
                failed.append(
                    f"group {group_key} committed ${group_committed:.2f} >= "
                    f"${self.limits.max_group_usd:.2f} correlated-print cap"
                )
            else:
                size = min(size, group_headroom)
                passed.append(
                    f"group {group_key} headroom: ${group_headroom:.2f} of "
                    f"${self.limits.max_group_usd:.2f}"
                )

        # 8. Daily loss limit
        if self._daily_pnl <= -self.limits.max_daily_loss_usd:
            failed.append(
                f"daily loss ${self._daily_pnl:.2f} exceeds "
                f"-${self.limits.max_daily_loss_usd:.0f} limit"
            )
        else:
            passed.append(f"daily P&L: ${self._daily_pnl:+.2f}")

        # 9. Drawdown check
        drawdown = 1 - (self._bankroll / self._peak_bankroll) if self._peak_bankroll > 0 else 0
        if drawdown >= self.limits.max_drawdown_pct:
            failed.append(f"drawdown {drawdown:.0%} >= {self.limits.max_drawdown_pct:.0%} limit")
        else:
            passed.append(f"drawdown: {drawdown:.1%}")

        # 10. Liquidity
        if market_liquidity > 0 and market_liquidity < self.limits.min_liquidity_usd:
            failed.append(
                f"liquidity ${market_liquidity:.0f} < ${self.limits.min_liquidity_usd:.0f}"
            )
        else:
            passed.append("liquidity OK")

        # 11. Time to deadline
        if days_to_deadline < self.limits.min_days_to_deadline:
            failed.append(
                f"{days_to_deadline}d to deadline < {self.limits.min_days_to_deadline}d minimum"
            )
        else:
            passed.append(f"{days_to_deadline}d to deadline")

        # Final decision
        approved = len(failed) == 0 and size > 0.01
        if approved:
            size = round(size, 2)

        reason = "APPROVED" if approved else "REJECTED: " + "; ".join(failed)

        return RiskCheckResult(
            approved=approved,
            reason=reason,
            adjusted_size_usd=size if approved else 0.0,
            checks_passed=passed,
            checks_failed=failed,
        )

    def record_trade_pnl(self, pnl: float) -> None:
        """Record realized P&L from a trade."""
        self._maybe_reset_daily()
        self._daily_pnl += pnl
        self._bankroll += pnl
        self._peak_bankroll = max(self._peak_bankroll, self._bankroll)

    def seed_exposure(self, total_usd: float) -> None:
        """Restore exposure this process did not itself accumulate.

        Every `RiskManager` starts a fresh process at `_total_exposure ==
        0.0`, but real GTC orders resting at the venue -- or commitments
        already recorded in this book by an earlier process -- still count
        against the venue's real risk (audit finding C2). Call this once
        at startup with `load_open_commitments(db_path)["total_usd"]`
        before evaluating any intent.

        `max`, deliberately never a plain assignment or `+=`: at startup
        `_total_exposure` is 0, so `max(0, total_usd) == total_usd` either
        way. But `add_exposure` may already have run for trades THIS
        process placed since the last seed (or since construction) -- a
        re-seed must be idempotent and must never claw back exposure this
        process has already observed and added on top of the seed.
        """
        self._total_exposure = max(self._total_exposure, total_usd)

    def group_exposure(self) -> dict[str, float]:
        """Read-only snapshot of the live per-group ledger (H5, Task 3.6;
        controller ruling R16 fix for finding I-1).

        This is the COMMITTED BOOK per correlated print, not just this
        process's own placements: whatever `seed_group_exposure` restored
        from the DB's open book (a prior process's, or the venue's,
        resting commitments) MAX'd against whatever `add_exposure(...,
        group_key=...)` has added since. A run's printed report should
        read this (see `src.trading.executor.render_group_commitments`)
        rather than summing its own decisions, which can never show a
        commitment dedup correctly kept out of that run altogether (e.g.
        the three resting gtm:2026-12 orders, $29.64).

        A COPY, not the live dict -- callers must not be able to mutate
        `RiskManager` state through the object this returns.
        """
        return dict(self._group_exposure)

    def seed_group_exposure(self, by_group: dict[str, float]) -> None:
        """Restore per-group commitments this process did not itself accumulate.

        The group-cap analogue of `seed_exposure` (H5, Task 3.6): call it
        once at startup with `load_open_commitments(db_path)["by_group"]`
        so a fresh process inherits what the DB's open book already has
        riding on each correlated print, not just the total. `max` per
        group, for the identical idempotence reason as `seed_exposure`: a
        re-seed must never claw back group exposure this process has
        already added via `add_exposure(..., group_key=...)` for trades
        placed since the last seed.
        """
        for group_key, amount in by_group.items():
            self._group_exposure[group_key] = max(
                self._group_exposure.get(group_key, 0.0), amount
            )

    def add_exposure(self, amount: float, group_key: str = "") -> None:
        """Track that we've opened a new position.

        `group_key` (H5, Task 3.6) additionally attributes `amount` to a
        correlated-group ledger, e.g. "gtm:2026-12" -- so THIS RUN's
        accepted intents count against `max_group_usd` together with
        whatever `seed_group_exposure` restored from the DB's open book
        (the missing-position-dedup lesson: a cap must count the resting
        book plus this run's own adds, not either one alone). "" (the
        default) means "no group": total exposure still moves, but no
        group bucket does -- matching `check_trade`'s "" semantics.
        """
        self._total_exposure += amount
        if group_key:
            self._group_exposure[group_key] = (
                self._group_exposure.get(group_key, 0.0) + amount
            )

    def remove_exposure(self, amount: float) -> None:
        """Track that a position has been closed."""
        self._total_exposure = max(0, self._total_exposure - amount)

    def _maybe_reset_daily(self) -> None:
        """Reset daily P&L at midnight."""
        today = datetime.now().date()
        if today > self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
