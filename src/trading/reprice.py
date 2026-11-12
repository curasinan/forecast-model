"""Cancel resting paper orders priced below today's limit so they can be placed again."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.domain.ids import MarketId
from src.trading.executor import TradeDecision, TradeIntent, TradingExecutor
from src.trading.order_manager import snap_to_tick
from src.trading.reconcile import drop_already_committed

logger = logging.getLogger(__name__)

PRICE_TOL = 1e-9


@dataclass
class RepriceCandidate:
    """One resting book order and the intent that would replace it at a higher limit."""

    row: dict
    intent: TradeIntent
    con_id: int
    new_price: float

    @property
    def perm_id(self) -> str:
        return str(self.row["perm_id"])

    @property
    def question_id(self) -> str:
        return self.intent.edge.question_id


@dataclass
class RepriceOutcome:
    """What a reprice pass cancelled, what it left alone, and why."""

    cancelled_perm_ids: set[str] = field(default_factory=set)
    replacements: frozenset[str] = frozenset()
    records: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resting_book_rows(db_path: str) -> list[dict]:
    """Book orders still resting with nothing filled and a permId to find them by."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT order_id, question_id, outcome, price, size, cost_usd, market_id, "
            "venue_order_id, perm_id, filled_size FROM trade_log "
            "WHERE status = 'placed' AND COALESCE(filled_size, 0) = 0 "
            "AND COALESCE(perm_id, '') != '' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def intent_leg(intent: TradeIntent) -> tuple[int, float] | None:
    """(conId, limit) of the leg this intent would buy, the limit as the order will carry it."""
    if intent.edge.edge_pct > 0:
        leg_id, price = intent.market.market_id, intent.edge.market_prob
    else:
        leg_id, price = intent.market.no_id, intent.no_price
    if leg_id is None or price is None:
        return None
    try:
        con_id = leg_id.con_id
    except ValueError:
        return None
    tick = intent.market.tick_size
    if tick and tick > 0:
        price = snap_to_tick(price, tick)
    return con_id, float(price)


def plan_reprices(intents: list[TradeIntent], book_rows: list[dict]) -> list[RepriceCandidate]:
    """Pair each resting book order with an intent on the same leg whose limit is higher."""
    by_con: dict[int, tuple[TradeIntent, float]] = {}
    for intent in intents:
        leg = intent_leg(intent)
        if leg is not None and leg[0] not in by_con:
            by_con[leg[0]] = (intent, leg[1])

    candidates: list[RepriceCandidate] = []
    for row in book_rows:
        try:
            con_id = MarketId.parse(row["market_id"] or "").con_id
        except (ValueError, TypeError):
            continue
        match = by_con.get(con_id)
        if match is None:
            continue
        intent, new_price = match
        if float(row["price"]) < new_price - PRICE_TOL:
            candidates.append(RepriceCandidate(row=row, intent=intent, con_id=con_id,
                                               new_price=new_price))
    return candidates


def release_commitments(commitments: dict, rows: list[dict]) -> dict:
    """`commitments` as they would read with `rows` cancelled."""
    total = float(commitments.get("total_usd", 0.0))
    by_market = dict(commitments.get("by_market", {}))
    by_group = dict(commitments.get("by_group", {}))
    by_group_counts = dict(commitments.get("by_group_counts", {}))
    for row in rows:
        amount = float(row.get("cost_usd") or 0.0)
        total -= amount
        market_id = row.get("market_id") or ""
        if market_id in by_market:
            by_market[market_id] -= amount
            if by_market[market_id] <= PRICE_TOL:
                del by_market[market_id]
        group = str(row.get("question_id") or "").rsplit(":", 1)[0]
        if group in by_group:
            by_group[group] = max(0.0, by_group[group] - amount)
            by_group_counts[group] = by_group_counts.get(group, 1) - 1
            if by_group_counts[group] <= 0:
                del by_group[group]
                del by_group_counts[group]
    return {"total_usd": max(0.0, total), "by_market": by_market, "by_group": by_group,
            "by_group_counts": by_group_counts}


def _preview(executor: TradingExecutor, intents: list[TradeIntent], resting: list,
             commitments: dict, chosen: list[RepriceCandidate]) -> dict[str, TradeDecision]:
    """Decisions per question_id if exactly `chosen` were cancelled first."""
    perms = {c.perm_id for c in chosen}
    resting_view = [o for o in resting if str((o.venue_meta or {}).get("perm_id") or "") not in perms]
    released = release_commitments(commitments, [c.row for c in chosen])
    kept = drop_already_committed(list(intents), resting_view, released)
    return {d.question_id: d for d in executor.preview_intents(kept, commitments=released)}


def _replaced(decision: TradeDecision | None) -> bool:
    return (decision is not None and decision.risk_check.approved
            and decision.order is not None and decision.order.size > 0)


def _why_not(decision: TradeDecision | None) -> str:
    if decision is None:
        return "no decision for it"
    return decision.reason or "refused by the risk checks"


def _record(c: RepriceCandidate, status: str, decision: TradeDecision | None, note: str = "") -> dict:
    replaced = _replaced(decision)
    return {
        "order_id": c.row["order_id"], "perm_id": c.perm_id, "question_id": c.question_id,
        "old_price": float(c.row["price"]), "old_size": float(c.row["size"]),
        "new_price": c.new_price,
        "new_size": decision.order.size if replaced else None,
        "replaced": replaced, "status": status, "note": note,
    }


def _prompt(c: RepriceCandidate, decision: TradeDecision | None) -> str:
    head = (f"\nREPRICE {c.question_id}: cancel BUY {float(c.row['size']):g} "
            f"@ {float(c.row['price']):g} (order {c.row['order_id']})")
    if _replaced(decision):
        return f"{head} and buy {decision.order.size:g} @ {decision.order.price:g} instead? [y/N]: "
    return (f"{head} with NO replacement ({_why_not(decision)}), freeing "
            f"${float(c.row['cost_usd'] or 0.0):.2f} for the higher-edge strikes? [y/N]: ")


def _mark_cancelled(db_path: str, c: RepriceCandidate, ts: str, replaced: bool) -> None:
    what = f"to buy again at {c.new_price:g}" if replaced else "with no replacement under the caps"
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                "UPDATE trade_log SET status = 'cancelled', last_synced_utc = ?, "
                "reason = COALESCE(reason, '') || ? WHERE order_id = ? AND status = 'placed'",
                (ts, f" | repriced {ts}: cancelled at the venue {what}", c.row["order_id"]),
            )
    finally:
        conn.close()


def reprice_resting_orders(
    executor: TradingExecutor,
    venue,
    db_path: str,
    intents: list[TradeIntent],
    resting: list,
    commitments: dict,
    *,
    mode: str = "approve",
    approval_input: Callable[[str], str] = input,
    now: datetime | None = None,
) -> RepriceOutcome:
    """Cancel each resting order priced below the limit an intent would now pay for its leg.

    The replacements are placed afterwards by the caller, highest edge first,
    as far as the caps allow; a dry run with every cancellation in place says
    beforehand which orders get one, and approve mode names those that do
    not. A book row is marked cancelled only after the venue confirms the
    cancel, so the daily sync does not read the gap as a venue failure.
    """
    outcome = RepriceOutcome()
    candidates = plan_reprices(intents, resting_book_rows(db_path))
    if not candidates:
        return outcome

    decisions = _preview(executor, intents, resting, commitments, candidates)
    chosen = candidates
    if mode == "approve":
        chosen = []
        for c in candidates:
            try:
                answer = approval_input(_prompt(c, decisions.get(c.question_id))).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer in ("y", "yes"):
                chosen.append(c)
            else:
                outcome.records.append(_record(c, "kept", None, "operator declined"))
        if len(chosen) != len(candidates):
            decisions = _preview(executor, intents, resting, commitments, chosen) if chosen else {}

    if not chosen:
        return outcome

    own = {str((o.venue_meta or {}).get("perm_id") or ""): o for o in venue.get_open_orders()}
    ts = (now or datetime.now(timezone.utc)).isoformat()
    replaced: set[str] = set()
    for c in chosen:
        d = decisions.get(c.question_id)
        venue_order = own.get(c.perm_id)
        if venue_order is None:
            outcome.notes.append(
                f"{c.question_id}: order {c.row['order_id']} (permId {c.perm_id}) is not among "
                "this session's open orders -- placed by another client id, or no longer "
                "resting; kept, nothing cancelled"
            )
            outcome.records.append(_record(c, "kept", None, "not among this session's open orders"))
            continue
        if not venue.cancel_order(venue_order.order_id):
            outcome.notes.append(
                f"{c.question_id}: cancel of order {c.row['order_id']} (permId {c.perm_id}) not "
                "confirmed (it may have filled); kept in the book, no replacement -- the next "
                "sync records what happened"
            )
            outcome.records.append(_record(c, "kept", None, "cancel not confirmed"))
            continue
        _mark_cancelled(db_path, c, ts, _replaced(d))
        outcome.cancelled_perm_ids.add(c.perm_id)
        outcome.records.append(_record(c, "cancelled", d))
        if _replaced(d):
            replaced.add(c.question_id)
            logger.info("repriced %s: cancelled %s @ %g, replacement %g @ %g", c.question_id,
                        c.row["order_id"], float(c.row["price"]), d.order.size, d.order.price)
        else:
            outcome.notes.append(
                f"{c.question_id}: order {c.row['order_id']} cancelled with no replacement "
                f"({_why_not(d)})"
            )

    outcome.replacements = frozenset(replaced)
    return outcome


__all__ = [
    "RepriceCandidate",
    "RepriceOutcome",
    "intent_leg",
    "plan_reprices",
    "release_commitments",
    "reprice_resting_orders",
    "resting_book_rows",
]
