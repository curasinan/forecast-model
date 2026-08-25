"""Durable reconciliation of the IBKR paper book against venue truth.

A GTC paper order rests at ForecastEx for days or weeks. Order state used
to live only in the placing process, so a fill that landed after that
process exited was never recorded anywhere: the book could not state its
own results. This module closes that hole. It reads what the venue says
(resting orders + current positions) and writes the consequences into
`ibkr_paper.db` -- `trade_log` status/`filled_size`, one `fill_events` row
per newly-observed cumulative fill, and the derived `positions` row for an
order that completed -- so a 365-day paper measurement survives restarts.

Pure logic, duck-typed venue: anything exposing

    get_all_open_orders() -> list[src.domain.models.Order]
    get_positions()       -> list[src.domain.models.Position]

works (the IBKR adapter in production, a list-backed fake in tests). No
venue SDK is imported here -- reconciliation lives outside `src/venues/`
and consumes only `src.domain` types plus sqlite3.

Join key (controller ruling R1): the venue's permId, normalized to `str`
on BOTH sides -- `str(order.venue_meta.get("perm_id") or "")` against the
TEXT `trade_log.perm_id`. IBKR order ids are per-client and meaningless
across sessions; permId is the only stable identity. A book row with no
permId on file is SKIPPED with a note (surfaced, never guessed) -- fix it
with `scripts/migrations/migrate_ibkr_paper_v2.py`, not by inference here.

A run happens in two phases inside ONE transaction: every conclusion is
planned first (reads only), the safety guards below judge the plan as a
whole, and only then is anything written. That ordering is what makes a
mass-cancellation abort possible at all.

Ground rules this module obeys:

- It never places, cancels, or modifies anything at the venue. It only
  reads venue state and writes the book.
- Venue orders it does not recognise (foreign/manual, another client id)
  are counted and named in the report, never touched, never written.
- It never invents a number. Where venue state and recorded state
  disagree, the recorded fills stay authoritative and the disagreement is
  reported as a note for an operator to resolve.
- Every write in a run happens in ONE transaction on ONE connection: a
  failure partway through leaves the book exactly as it was.

Position attribution (controller ruling R7 + the C-1 fix): a venue
position is the CUMULATIVE holding in one leg contract and says nothing
about WHICH order produced it. An order is only concluded FILLED against
the quantity still AVAILABLE in that contract -- the venue quantity minus
what open `positions` rows already attribute to other orders, minus what
earlier rows in this same run claimed. Without that subtraction a position
held for weeks would book every later order on the same contract as a
full fill, inventing fills that never happened.

Mass-cancellation guard (controller ruling R8): a degraded Gateway session
answers `reqAllOpenOrders()` with an empty list, which is indistinguishable
from "every order was cancelled". Concluding on that would wipe the book in
one run. So the whole run ABORTS -- zero writes, `report.aborted = True`,
and a loud note -- when either (a) any cancellation is planned while the
venue's open-order list is EMPTY, or (b) more than half of at least two
candidate rows would be cancelled. FILLED conclusions are unaffected: an
all-filled book legitimately shows an empty open-order list and must not
deadlock. `allow_mass_cancel=True` disables both guards for an operator
who has confirmed the cancellations at the venue.

Timestamp/commission semantics (operator-approved 2026-08-25; the full
note and the single source of truth for the `fill_events` DDL live in
`src.trading.paper_schema`):

- `detected_ts_utc` is the sync DETECTION time, always set. On the
  position-diff path the TRUE fill time is unknown -- a fill that landed
  days ago is only detected at the next sync. NEITHER `detected_ts_utc`
  NOR `ts_utc` may be treated as the fill time in P&L timing analysis.
- `ts_utc` is the best-known event time; for rows this module writes it
  equals `detected_ts_utc`. A future execution-report enrichment may set
  it to the venue execution time instead.
- `commission_usd` is written NULL. The column exists so a later
  `reqExecutions`/`CommissionReport` enrichment needs no new migration.

Idempotence rests on the `fill_events` primary key `(perm_id, cum_filled)`
plus `INSERT OR IGNORE`: re-running against unchanged venue state adds
zero rows and changes no status. Only `last_synced_utc` is refreshed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.domain.ids import IBKR, MarketId
from src.trading.paper_schema import FILL_EVENTS_DDL

if TYPE_CHECKING:  # pragma: no cover -- type-checking only, no runtime import
    from src.domain.models import Order
    from src.trading.executor import TradeIntent

logger = logging.getLogger(__name__)

# Contract quantities are whole numbers at ForecastEx; the tolerance only
# guards float round-tripping through SQLite REAL columns.
QTY_TOL = 1e-9

# Book rows that are still "working" and therefore worth reconciling.
CANDIDATE_STATUSES = ("placed", "partially_filled")

# Fraction of candidate rows whose cancellation in one run is treated as a
# venue-side failure rather than reality (ruling R8).
MASS_CANCEL_FRACTION = 0.5

_REQUIRED_TRADE_LOG_COLUMNS = {
    "perm_id", "filled_size", "last_synced_utc", "market_id", "venue",
}
_REQUIRED_POSITIONS_COLUMNS = {"order_id", "market_id", "venue"}


@dataclass
class ReconcileReport:
    """What one reconciliation run saw and did.

    `examined` counts every candidate `trade_log` row loaded (status
    'placed' or 'partially_filled'). The outcome buckets sum to `examined`
    MINUS any rows that had to be skipped (no permId on file, or an
    unparseable `market_id`); each such row is named in `notes`, so the
    difference is never silent.

    `aborted` is True when the mass-cancellation guard (ruling R8) stopped
    the run: NOTHING was written, every outcome bucket is zero, and `notes`
    carries the reason and how to override it. `examined` and
    `unmatched_venue_orders` still describe what was observed.
    """

    examined: int = 0
    filled: int = 0
    partially_filled: int = 0
    cancelled: int = 0
    unchanged: int = 0
    unmatched_venue_orders: int = 0
    notes: list[str] = field(default_factory=list)
    aborted: bool = False


@dataclass
class _Conclusion:
    """One planned outcome for one book row. Nothing here has been written."""

    row: sqlite3.Row
    perm_id: str
    kind: str  # 'unchanged' | 'partially_filled' | 'filled' | 'cancelled' | 'skipped'
    status: str | None = None                  # new trade_log status ('unchanged' -> None)
    filled_size: float | None = None           # new trade_log.filled_size, if it moves
    event: tuple | None = None                 # (cum_filled, fill_delta, avg_price, source)
    position: tuple | None = None              # (entry_price, size) for a derived position
    notes: list[str] = field(default_factory=list)


class _AbortRun(Exception):
    """Internal: unwind the transaction so an aborted run writes nothing."""


def reconcile_paper_book(db_path: str, venue, now: datetime | None = None, *,
                         allow_mass_cancel: bool = False) -> ReconcileReport:
    """Reconcile the paper book at `db_path` against `venue`'s state.

    Args:
        db_path: the `ibkr_paper.db` SQLite file (tests: a tmp copy only).
        venue: any object with `get_all_open_orders()` and `get_positions()`.
        now: detection timestamp; defaults to UTC now. Used for `ts_utc`,
            `detected_ts_utc`, `last_synced_utc` and derived `opened_at`.
        allow_mass_cancel: disable the R8 mass-cancellation guards. Only for
            an operator who has confirmed the cancellations at the venue --
            the default refuses to act on a venue answer that looks like a
            dropped session.

    Returns:
        A `ReconcileReport`; `aborted=True` means a guard fired and nothing
        was written. Raises whatever the venue or SQLite raises -- loudly,
        with the whole transaction rolled back.
    """
    stamp = now or datetime.now(timezone.utc)
    ts = stamp.isoformat()

    report = ReconcileReport()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        # autocommit=False (3.12+) keeps the CREATE TABLE below inside the same
        # transaction as the DML -- the pattern proven by migrate_ibkr_paper_v2.
        conn.autocommit = False
        try:
            with conn:
                _require_schema(conn)
                # Controller ruling R2: a book that has not been through the Task-1.4
                # migration still reconciles. The migration remains the official path.
                conn.execute(FILL_EVENTS_DDL)
                _reconcile(conn, venue, ts, stamp, report, allow_mass_cancel)
        except _AbortRun:
            # The `with` block rolled the transaction back on the way out, so
            # not even the CREATE TABLE survives. The report says why.
            logger.error("reconcile ABORTED without writing: %s", report.notes[-1])
    finally:
        conn.close()

    logger.info(
        "reconcile: aborted=%s examined=%d filled=%d partial=%d cancelled=%d unchanged=%d "
        "unmatched_venue_orders=%d notes=%d",
        report.aborted, report.examined, report.filled, report.partially_filled,
        report.cancelled, report.unchanged, report.unmatched_venue_orders, len(report.notes),
    )
    return report


# ---------------------------------------------------------------- internals
def _require_schema(conn: sqlite3.Connection) -> None:
    """Fail loudly (before any write) if the book predates the v2 columns."""
    for table, required in (("trade_log", _REQUIRED_TRADE_LOG_COLUMNS),
                            ("positions", _REQUIRED_POSITIONS_COLUMNS)):
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            raise RuntimeError(
                f"{table} table missing -- this is not an ibkr_paper book"
            )
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                f"{table} is missing column(s) {missing}: run "
                "scripts/migrations/migrate_ibkr_paper_v2.py before reconciling"
            )


def _perm_key(obj) -> str:
    """R1 normalization: venue permIds are ints, the DB column is TEXT."""
    return str((obj.venue_meta or {}).get("perm_id") or "")


def _index_open_orders(orders) -> tuple[dict[str, object], list, list[str]]:
    """Index venue orders by normalized permId.

    Returns (by_perm, without_perm_id, notes). Orders with no permId cannot
    be joined to anything; they are handed back for the caller to count and
    name rather than being dropped.
    """
    by_perm: dict[str, object] = {}
    without: list = []
    notes: list[str] = []
    for order in orders:
        key = _perm_key(order)
        if not key:
            without.append(order)
            continue
        if key in by_perm:
            notes.append(
                f"venue returned two open orders with permId {key}: keeping the "
                "first, verify at the venue"
            )
            continue
        by_perm[key] = order
    return by_perm, without, notes


def _index_positions(positions) -> tuple[dict[int, object], list[str]]:
    """Index venue positions by IBKR conId (the identity of one LEG contract)."""
    by_con: dict[int, object] = {}
    notes: list[str] = []
    for pos in positions:
        if pos.market_id.venue != IBKR:
            notes.append(f"ignoring non-IBKR venue position {pos.market_id}")
            continue
        con_id = pos.market_id.con_id
        if con_id in by_con:
            notes.append(
                f"venue returned two positions for conId {con_id}: keeping the "
                "first, verify at the venue"
            )
            continue
        by_con[con_id] = pos
    return by_con, notes


def _reconcile(conn: sqlite3.Connection, venue, ts: str, stamp: datetime,
               report: ReconcileReport, allow_mass_cancel: bool) -> None:
    placeholders = ",".join("?" * len(CANDIDATE_STATUSES))
    rows = conn.execute(
        "SELECT rowid AS rid, order_id, question_id, outcome, price, size, cost_usd, "
        "status, market_id, venue, filled_size, perm_id FROM trade_log "
        f"WHERE status IN ({placeholders}) ORDER BY rowid",
        CANDIDATE_STATUSES,
    ).fetchall()
    report.examined = len(rows)

    venue_orders = venue.get_all_open_orders()
    open_by_perm, orders_without_perm, notes = _index_open_orders(venue_orders)
    report.notes.extend(notes)

    # Positions are fetched lazily: a run where every order is still resting
    # never needs them.
    position_index: dict[int, object] | None = None

    def positions() -> dict[int, object]:
        nonlocal position_index
        if position_index is None:
            position_index, pos_notes = _index_positions(venue.get_positions())
            report.notes.extend(pos_notes)
        return position_index

    # How many contracts this run has already attributed to each conId's
    # venue position (see the module docstring on attribution).
    claimed: dict[int, float] = {}

    # -- phase 1: plan (reads only) --------------------------------------
    plans = [_plan_row(conn, row, open_by_perm, positions, claimed) for row in rows]

    _note_unmatched_venue_orders(conn, open_by_perm, orders_without_perm, report)

    if not allow_mass_cancel:
        reason = _mass_cancel_reason(plans, len(rows), len(venue_orders))
        if reason is not None:
            report.aborted = True
            report.notes.append(f"ABORTED, nothing written: {reason}")
            raise _AbortRun

    # -- phase 2: write --------------------------------------------------
    _execute(conn, plans, ts, stamp, report)
    _note_over_attribution(position_index, claimed, report)


def _mass_cancel_reason(plans: list[_Conclusion], candidates: int,
                        venue_order_count: int) -> str | None:
    """Ruling R8: is this plan more likely a venue failure than reality?"""
    cancels = sum(1 for plan in plans if plan.kind == "cancelled")
    if cancels == 0:
        return None
    if venue_order_count == 0:
        return (
            f"the venue returned an EMPTY open-order list and {cancels} of {candidates} "
            "candidate row(s) would be cancelled. A dropped or degraded Gateway session "
            "looks exactly like this. Re-run once the session is healthy, or pass "
            "allow_mass_cancel=True after confirming the cancellations at the venue"
        )
    if candidates >= 2 and cancels / candidates > MASS_CANCEL_FRACTION:
        return (
            f"{cancels} of {candidates} candidate rows would be cancelled in one run "
            f"(over {MASS_CANCEL_FRACTION:.0%}), which is more likely a partial venue "
            "answer than a real mass cancellation. Re-run, or pass "
            "allow_mass_cancel=True after confirming the cancellations at the venue"
        )
    return None


def _execute(conn: sqlite3.Connection, plans: list[_Conclusion], ts: str, stamp: datetime,
             report: ReconcileReport) -> None:
    """Apply the planned conclusions. Every write in the run happens here,
    inside the caller's single transaction.
    """
    for plan in plans:
        report.notes.extend(plan.notes)
        if plan.kind == "skipped":
            continue

        order_id = plan.row["order_id"]
        if plan.event is not None:
            cum_filled, fill_delta, avg_price, source = plan.event
            _append_fill_event(
                conn, perm_id=plan.perm_id, order_id=order_id, ts=ts,
                cum_filled=cum_filled, fill_delta=fill_delta, avg_price=avg_price,
                source=source,
            )

        if plan.status is None:
            conn.execute(
                "UPDATE trade_log SET last_synced_utc = ? WHERE order_id = ?",
                (ts, order_id),
            )
        elif plan.filled_size is None:
            conn.execute(
                "UPDATE trade_log SET status = ?, last_synced_utc = ? WHERE order_id = ?",
                (plan.status, ts, order_id),
            )
        else:
            conn.execute(
                "UPDATE trade_log SET status = ?, filled_size = ?, last_synced_utc = ? "
                "WHERE order_id = ?",
                (plan.status, plan.filled_size, ts, order_id),
            )

        if plan.position is not None:
            entry_price, size = plan.position
            _open_derived_position(conn, plan.row, entry_price=entry_price, size=size,
                                   stamp=stamp)

        setattr(report, plan.kind, getattr(report, plan.kind) + 1)


# ---------------------------------------------------------------- planning
def _plan_row(conn, row, open_by_perm, positions, claimed) -> _Conclusion:
    order_id = row["order_id"]
    perm_id = str(row["perm_id"] or "")
    if not perm_id:
        return _Conclusion(row=row, perm_id="", kind="skipped", notes=[
            f"trade_log {order_id}: no perm_id on file -- skipped, never guessed "
            "(backfill with scripts/migrations/migrate_ibkr_paper_v2.py)"
        ])

    size = float(row["size"] or 0.0)
    recorded = float(row["filled_size"] or 0.0)
    venue_order = open_by_perm.get(perm_id)

    if venue_order is not None:
        return _plan_resting(row, venue_order, perm_id, size, recorded)

    try:
        con_id = MarketId.parse(row["market_id"] or "").con_id
    except (ValueError, TypeError) as exc:
        return _Conclusion(row=row, perm_id=perm_id, kind="skipped", notes=[
            f"trade_log {order_id}: cannot read a conId from market_id "
            f"{row['market_id']!r} ({exc}) -- skipped, never guessed"
        ])

    position = positions().get(con_id)
    quantity = float(position.quantity) if position is not None else 0.0
    available = _available_quantity(conn, row, con_id, position, claimed)

    # A venue position is the CUMULATIVE holding in that leg contract, so the
    # covering test is against the full order size (ruling R7, never
    # size-minus-recorded) -- but only against the part of that position not
    # already attributed to another order (the C-1 fix).
    if position is not None and available >= size - QTY_TOL:
        claimed[con_id] = claimed.get(con_id, 0.0) + size
        return _plan_filled(row, perm_id, size, recorded, position, quantity, con_id)

    plan = _plan_cancelled(conn, row, perm_id, size, recorded, position, quantity,
                           available, con_id)
    if recorded > QTY_TOL:
        claimed[con_id] = claimed.get(con_id, 0.0) + recorded
    return plan


def _available_quantity(conn, row, con_id: int, position, claimed: dict[int, float]) -> float:
    """Contracts in this leg's venue position not already spoken for.

    Subtracts (a) every OPEN `positions` row for the same market that belongs
    to a different order -- fills booked by earlier runs, which the venue
    position still includes -- and (b) what earlier rows in this run claimed.
    Without (a), a position held to resolution would book every later order on
    the same contract as a full fill (C-1).
    """
    if position is None:
        return 0.0
    booked = conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM positions "
        "WHERE market_id = ? AND closed_at IS NULL "
        "AND (order_id IS NULL OR order_id != ?)",
        (row["market_id"], row["order_id"]),
    ).fetchone()[0]
    return float(position.quantity) - float(booked or 0.0) - claimed.get(con_id, 0.0)


def _plan_resting(row, venue_order, perm_id, size, recorded) -> _Conclusion:
    """The order is still resting at the venue: only a fill delta matters."""
    order_id = row["order_id"]
    venue_filled = float(venue_order.filled_quantity or 0.0)

    if venue_filled > recorded + QTY_TOL:
        notes = []
        if venue_filled >= size - QTY_TOL:
            notes.append(
                f"trade_log {order_id} (permId {perm_id}): venue reports {venue_filled:g} "
                f"of {size:g} filled but the order is still in the open list -- left "
                "'partially_filled'; the next sync closes it out"
            )
        # avg_price caveat: IB reports avgCost, which for a multiplier-1
        # ForecastEx OPT contract is ~= the per-contract price. [OPERATOR]
        # verifies this against the first real fill statement (Task 1.9).
        return _Conclusion(
            row=row, perm_id=perm_id, kind="partially_filled",
            status="partially_filled", filled_size=venue_filled,
            event=(venue_filled, venue_filled - recorded, venue_order.avg_fill_price,
                   "sync_open_order"),
            notes=notes,
        )

    notes = []
    if venue_filled < recorded - QTY_TOL:
        notes.append(
            f"trade_log {order_id} (permId {perm_id}): venue reports {venue_filled:g} "
            f"filled but {recorded:g} are already recorded -- recorded fills kept, "
            "verify at the venue"
        )
    return _Conclusion(row=row, perm_id=perm_id, kind="unchanged", notes=notes)


def _plan_filled(row, perm_id, size, recorded, position, quantity, con_id) -> _Conclusion:
    """Gone from the open list, and the contract still has room for this
    order's whole size: FILLED.
    """
    entry_price = float(position.avg_price)
    notes = []
    if quantity > size + QTY_TOL:
        # I-2: per-order attribution is not knowable from get_positions(), so
        # say plainly that the price is a blend rather than pretending.
        notes.append(
            f"trade_log {row['order_id']} (permId {perm_id}): venue position in conId "
            f"{con_id} holds {quantity:g} contracts against this order's {size:g} -- "
            f"entry price {entry_price:g} is the venue's BLENDED average over every fill "
            "in that contract, not this order's own fill price; verify against the "
            "statement before using it for per-order P&L"
        )
    delta = size - recorded
    return _Conclusion(
        row=row, perm_id=perm_id, kind="filled", status="filled", filled_size=size,
        event=((size, delta, position.avg_price, "sync_position_diff")
               if delta > QTY_TOL else None),
        position=(entry_price, size),
        notes=notes,
    )


def _plan_cancelled(conn, row, perm_id, size, recorded, position, quantity, available,
                    con_id) -> _Conclusion:
    """Gone from the open list with nothing left to cover it: CANCELLED.

    Recorded partial fills are never erased -- they are venue-confirmed
    history. The partial position they represent is opened at the partial
    size, and any disagreement with the venue's position is reported.
    """
    order_id = row["order_id"]
    notes: list[str] = []
    plan = _Conclusion(row=row, perm_id=perm_id, kind="cancelled", status="cancelled",
                       notes=notes)

    if recorded > QTY_TOL:
        if position is not None:
            entry_price = float(position.avg_price)
        else:
            recorded_price = _last_recorded_fill_price(conn, perm_id)
            entry_price = float(recorded_price if recorded_price is not None else row["price"])
            if recorded_price is None:
                notes.append(
                    f"trade_log {order_id} (permId {perm_id}): {recorded:g} contracts "
                    "recorded filled but no venue position and no priced fill_event -- "
                    "partial position opened at the order's limit price, verify it"
                )
        plan.position = (entry_price, recorded)

    if position is not None and available < size - QTY_TOL and quantity >= size - QTY_TOL:
        # The contract holds enough, but other orders already account for it.
        notes.append(
            f"trade_log {order_id} (permId {perm_id}): conId {con_id} holds {quantity:g} "
            f"contracts, but only {available:g} are unattributed (other book orders hold "
            f"the rest), so this order's {size:g} are NOT covered -- cancelled rather "
            "than crediting someone else's position; verify against the statement"
        )
    elif position is not None and abs(quantity - recorded) > QTY_TOL:
        notes.append(
            f"trade_log {order_id} (permId {perm_id}): venue position holds {quantity:g} "
            f"contracts in {row['market_id']} but {recorded:g} are recorded filled for "
            f"this order (ordered {size:g}) -- recorded fills kept, resolve manually"
        )
    elif position is None and recorded > QTY_TOL:
        notes.append(
            f"trade_log {order_id} (permId {perm_id}): {recorded:g} contracts recorded "
            f"filled but the venue reports no position in {row['market_id']} -- recorded "
            "fills kept, resolve manually"
        )
    return plan


# ---------------------------------------------------------------- writes
def _append_fill_event(conn, *, perm_id: str, order_id: str, ts: str, cum_filled: float,
                       fill_delta: float, avg_price, source: str) -> None:
    """One observed cumulative-fill level. `INSERT OR IGNORE` + the
    (perm_id, cum_filled) PK make re-runs no-ops (see module docstring for
    the ts/commission semantics).
    """
    conn.execute(
        """INSERT OR IGNORE INTO fill_events
           (perm_id, order_id, ts_utc, detected_ts_utc, cum_filled, fill_delta,
            avg_price, commission_usd, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
        (perm_id, order_id, ts, ts, float(cum_filled), float(fill_delta),
         None if avg_price is None else float(avg_price), source),
    )


def _last_recorded_fill_price(conn, perm_id: str):
    row = conn.execute(
        "SELECT avg_price FROM fill_events WHERE perm_id = ? AND avg_price IS NOT NULL "
        "ORDER BY cum_filled DESC LIMIT 1",
        (perm_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _open_derived_position(conn, row, *, entry_price: float, size: float,
                           stamp: datetime) -> None:
    """Insert the position an order's fills created.

    Mirrors `PositionTracker._open_position` (same columns, same
    idempotence key: one position per order_id) but inlined so that every
    write in a reconcile run shares this module's single connection and
    transaction -- PositionTracker opens its own.
    """
    exists = conn.execute(
        "SELECT 1 FROM positions WHERE order_id = ? LIMIT 1", (row["order_id"],)
    ).fetchone()
    if exists:
        return
    opened_at = _free_opened_at(conn, row["question_id"], row["outcome"], stamp)
    conn.execute(
        """INSERT INTO positions
           (question_id, outcome, entry_price, size, cost_basis, opened_at,
            market_id, venue, order_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["question_id"],
            row["outcome"],
            float(entry_price),
            float(size),
            round(float(entry_price) * float(size), 2),
            opened_at,
            row["market_id"] or "",
            row["venue"] or "",
            row["order_id"],
        ),
    )


def _free_opened_at(conn, question_id: str, outcome: str, stamp: datetime) -> str:
    """An `opened_at` that does not collide with an existing positions row.

    The legacy `positions` primary key is (question_id, outcome, opened_at),
    so two orders on the same leg concluded in the same run would collide on
    a single run timestamp and one would be lost. Later rows get the next
    free microsecond. That offset is a primary-key artifact, NOT a claim
    about fill timing -- the true fill time is unknown on this path (see the
    module's timestamp semantics note).
    """
    candidate = stamp
    for _ in range(1_000):
        iso = candidate.isoformat()
        hit = conn.execute(
            "SELECT 1 FROM positions WHERE question_id = ? AND outcome = ? "
            "AND opened_at = ? LIMIT 1",
            (question_id, outcome, iso),
        ).fetchone()
        if hit is None:
            return iso
        candidate = candidate + timedelta(microseconds=1)
    raise RuntimeError(
        f"cannot find a free opened_at for {question_id}/{outcome} at {stamp.isoformat()}"
    )


# ---------------------------------------------------------------- reporting
def _note_over_attribution(position_index, claimed: dict[int, float],
                           report: ReconcileReport) -> None:
    """Belt and braces on attribution: FILLED conclusions are capped by
    `_available_quantity`, but partial positions kept on the cancelled path
    come from recorded fills and are never trimmed. If the total this run
    attributed still exceeds the venue's holding, say so.
    """
    if not position_index:
        return
    for con_id, total in claimed.items():
        position = position_index.get(con_id)
        if position is None:
            continue
        quantity = float(position.quantity)
        if total > quantity + QTY_TOL:
            report.notes.append(
                f"conId {con_id}: venue position holds {quantity:g} contracts but this "
                f"run attributed {total:g} to book orders -- attribution ambiguous, "
                "verify against the venue statement"
            )


def _note_unmatched_venue_orders(conn, open_by_perm, orders_without_perm,
                                 report: ReconcileReport) -> None:
    """Resting venue orders this book does not know about.

    Matched against EVERY trade_log permId (not just the candidate rows), so
    an order whose book row is already closed is not mislabelled foreign.
    Foreign orders are counted and named -- never cancelled, never written.
    """
    known = {str(r[0] or "") for r in conn.execute("SELECT perm_id FROM trade_log")}
    known.discard("")

    for perm_id, order in open_by_perm.items():
        if perm_id in known:
            continue
        report.unmatched_venue_orders += 1
        report.notes.append(
            f"venue order permId {perm_id} ({order.request.side} {order.request.quantity:g} "
            f"of {order.request.market_id}) matches no trade_log row -- foreign or manual, "
            "left untouched"
        )

    for order in orders_without_perm:
        report.unmatched_venue_orders += 1
        report.notes.append(
            f"venue order {order.order_id} ({order.request.market_id}) carries no permId -- "
            "cannot be joined to the book, left untouched"
        )


# =================================================================== Task 1.7
# Placement idempotence + persistent exposure seeding (audit finding C2).
#
# A GTC paper order rests at the venue for days or weeks; nothing before
# this task stopped a re-run of `scripts/run_ibkr_paper.py` from placing
# that exact order a SECOND time, stacking duplicate exposure on a book
# that already has it working. `load_open_commitments` reads what the
# CURRENT process's own book already committed money to; `drop_already_
# committed` uses that (plus a fresh venue read) to refuse to re-place
# anything already covered. Ground rule: dedup only ever SKIPS a would-be
# placement -- it never touches, cancels, or modifies anything it finds
# resting (that is measurement state, not something to clean up).


def _group_key(question_id: str) -> str:
    """The group one question belongs to (verbatim contract, reused by a
    later cross-market exposure task): drop the trailing ':<strike>'
    segment. "gtm:2026-12:1.3" -> "gtm:2026-12". A question_id with no
    ':' (e.g. a legacy dash-separated id) is its own group unchanged.
    """
    return question_id.rsplit(":", 1)[0]


def load_open_commitments(db_path: str) -> dict:
    """Every dollar the book at `db_path` already has riding on the venue.

    Two sources, added without double-counting the same order twice:

    - `trade_log` rows with `status IN ('placed', 'partially_filled')`
      contribute their `cost_usd` -- the order's FULL intended commitment,
      not just what has filled so far. A 'placed' order rests at the venue
      for its whole size; a 'partially_filled' one is still working the
      remainder, so the venue-side risk is still the whole thing.
    - Open `positions` rows (`closed_at IS NULL`) contribute `cost_basis`,
      but ONLY for an `order_id` whose trade_log status is NOT itself
      'placed' or 'partially_filled'. A partially_filled order's own
      partial position is the FILLED SLICE of a commitment already
      counted in full above; adding it again would double-count that
      slice (a 9.84 commitment with a 2.40 partial position must total
      9.84, not 12.24). A row whose order finished (filled/cancelled) or
      has no trade_log row at all is not in that set, so its position
      counts normally.

    Group key: `_group_key(question_id)` -- see that function's contract.

    Returns `{"total_usd": float, "by_market": dict[str, float],
    "by_group": dict[str, float]}`. A missing `db_path` returns the same
    shape, all empty -- a book that has never been written has nothing
    committed, which is different from a read failure and must not raise.
    """
    empty: dict = {"total_usd": 0.0, "by_market": {}, "by_group": {}}
    if not os.path.exists(db_path):
        return empty

    by_market: dict[str, float] = {}
    by_group: dict[str, float] = {}
    total = 0.0

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row

        def _add(question_id: str, market_id: str, amount: float) -> None:
            nonlocal total
            total += amount
            if market_id:
                by_market[market_id] = by_market.get(market_id, 0.0) + amount
            by_group[_group_key(question_id)] = by_group.get(_group_key(question_id), 0.0) + amount

        placeholders = ",".join("?" * len(CANDIDATE_STATUSES))
        committed_order_ids: set[str] = set()
        for row in conn.execute(
            f"SELECT order_id, question_id, market_id, cost_usd FROM trade_log "
            f"WHERE status IN ({placeholders})",
            CANDIDATE_STATUSES,
        ):
            committed_order_ids.add(row["order_id"])
            _add(row["question_id"] or "", row["market_id"] or "", float(row["cost_usd"] or 0.0))

        for row in conn.execute(
            "SELECT order_id, question_id, market_id, cost_basis FROM positions "
            "WHERE closed_at IS NULL"
        ):
            if row["order_id"] in committed_order_ids:
                continue  # already counted in full via trade_log above (see docstring)
            _add(row["question_id"] or "", row["market_id"] or "", float(row["cost_basis"] or 0.0))
    finally:
        conn.close()

    return {"total_usd": total, "by_market": by_market, "by_group": by_group}


def _bought_leg_market_id(intent: "TradeIntent"):
    """Which contract this intent would BUY, or `None` if that cannot be
    resolved without guessing (an unresolvable NO leg).

    YES intent (edge_pct > 0): `market.market_id`. NO intent: `market.no_id`
    when the venue publishes a separate NO contract -- never the YES id and
    never invented, since IBKR's YES/NO legs are separate conIds.
    """
    if intent.edge.edge_pct > 0:
        return intent.market.market_id, "YES"
    return intent.market.no_id, "NO"


def drop_already_committed(intents: list["TradeIntent"], venue_open_orders: list["Order"],
                           commitments: dict) -> list["TradeIntent"]:
    """Drop any intent whose bought leg is already committed.

    An intent is dropped when its bought leg's IBKR conId either:
      (a) has a resting BUY order at the venue right now (`venue_open_orders`,
          e.g. `IBKRAdapter.get_all_open_orders()` -- a fresh read, not a
          cached one), or
      (b) already appears in `commitments["by_market"]` (see
          `load_open_commitments`) -- this process's OWN book already
          recorded a commitment for it, even if the venue read above
          somehow missed it.

    A NO intent whose market has no `no_id` cannot be resolved to a conId
    at all -- it is dropped with a warning rather than guessed (ground
    rule: never guess). `commitments["by_market"]` keys are `MarketId`
    strings like "ibkr:830396351"; a key that does not parse is skipped
    (tolerated, not fatal -- a foreign/legacy row must not crash dedup).

    Every drop is logged at WARNING with its reason. This function only
    ever removes candidates from the list it returns -- it never reads or
    writes anything else, and never touches `venue_open_orders` itself.
    """
    resting_buy_con_ids: set[int] = set()
    for order in venue_open_orders:
        if order.request.side != "BUY":
            continue
        try:
            resting_buy_con_ids.add(order.request.market_id.con_id)
        except ValueError:
            continue  # not an IBKR market id -- nothing to dedup against here

    committed_con_ids: set[int] = set()
    for market_id_str in commitments.get("by_market", {}):
        try:
            committed_con_ids.add(MarketId.parse(market_id_str).con_id)
        except (ValueError, TypeError):
            continue  # unparseable/non-IBKR key -- tolerated, not guessed

    kept: list["TradeIntent"] = []
    for intent in intents:
        question_id = intent.edge.question_id
        leg_id, leg_name = _bought_leg_market_id(intent)
        if leg_id is None:
            logger.warning(
                "dropping intent %s: NO leg has no resolvable market id (no `no_id` on "
                "the market) -- unresolvable, never guessed", question_id,
            )
            continue

        try:
            con_id = leg_id.con_id
        except ValueError:
            kept.append(intent)  # not an IBKR id -- this dedup has no opinion
            continue

        if con_id in resting_buy_con_ids:
            logger.warning(
                "dropping intent %s: %s leg (conId %s) already has a resting BUY order "
                "at the venue", question_id, leg_name, con_id,
            )
            continue
        if con_id in committed_con_ids:
            logger.warning(
                "dropping intent %s: %s leg (conId %s) already has a commitment in "
                "trade_log", question_id, leg_name, con_id,
            )
            continue
        kept.append(intent)

    return kept
