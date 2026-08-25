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
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.domain.ids import IBKR, MarketId
from src.trading.paper_schema import FILL_EVENTS_DDL

logger = logging.getLogger(__name__)

# Contract quantities are whole numbers at ForecastEx; the tolerance only
# guards float round-tripping through SQLite REAL columns.
QTY_TOL = 1e-9

# Book rows that are still "working" and therefore worth reconciling.
CANDIDATE_STATUSES = ("placed", "partially_filled")

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
    """

    examined: int = 0
    filled: int = 0
    partially_filled: int = 0
    cancelled: int = 0
    unchanged: int = 0
    unmatched_venue_orders: int = 0
    notes: list[str] = field(default_factory=list)


def reconcile_paper_book(db_path: str, venue, now: datetime | None = None) -> ReconcileReport:
    """Reconcile the paper book at `db_path` against `venue`'s state.

    Args:
        db_path: the `ibkr_paper.db` SQLite file (tests: a tmp copy only).
        venue: any object with `get_all_open_orders()` and `get_positions()`.
        now: detection timestamp; defaults to UTC now. Used for `ts_utc`,
            `detected_ts_utc`, `last_synced_utc` and derived `opened_at`.

    Returns:
        A `ReconcileReport`. Raises whatever the venue or SQLite raises --
        loudly, with the whole transaction rolled back.
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
        with conn:
            _require_schema(conn)
            # Controller ruling R2: a book that has not been through the Task-1.4
            # migration still reconciles. The migration remains the official path.
            conn.execute(FILL_EVENTS_DDL)
            _reconcile(conn, venue, ts, stamp, report)
    finally:
        conn.close()

    logger.info(
        "reconcile: examined=%d filled=%d partial=%d cancelled=%d unchanged=%d "
        "unmatched_venue_orders=%d notes=%d",
        report.examined, report.filled, report.partially_filled, report.cancelled,
        report.unchanged, report.unmatched_venue_orders, len(report.notes),
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
               report: ReconcileReport) -> None:
    placeholders = ",".join("?" * len(CANDIDATE_STATUSES))
    rows = conn.execute(
        "SELECT rowid AS rid, order_id, question_id, outcome, price, size, cost_usd, "
        "status, market_id, venue, filled_size, perm_id FROM trade_log "
        f"WHERE status IN ({placeholders}) ORDER BY rowid",
        CANDIDATE_STATUSES,
    ).fetchall()

    open_by_perm, orders_without_perm, notes = _index_open_orders(venue.get_all_open_orders())
    report.notes.extend(notes)

    # Positions are fetched lazily: a run where every order is still resting
    # never needs them, and a failure here must be able to roll back writes
    # already made in this transaction.
    position_index: dict[int, object] | None = None

    def positions() -> dict[int, object]:
        nonlocal position_index
        if position_index is None:
            position_index, pos_notes = _index_positions(venue.get_positions())
            report.notes.extend(pos_notes)
        return position_index

    # How many contracts this run attributed to each conId's venue position.
    claimed: dict[int, float] = {}

    for row in rows:
        report.examined += 1
        _reconcile_row(conn, row, open_by_perm, positions, claimed, ts, stamp, report)

    _note_over_attribution(position_index, claimed, report)
    _note_unmatched_venue_orders(conn, open_by_perm, orders_without_perm, report)


def _reconcile_row(conn, row, open_by_perm, positions, claimed, ts, stamp, report) -> None:
    order_id = row["order_id"]
    perm_id = str(row["perm_id"] or "")
    if not perm_id:
        report.notes.append(
            f"trade_log {order_id}: no perm_id on file -- skipped, never guessed "
            "(backfill with scripts/migrations/migrate_ibkr_paper_v2.py)"
        )
        return

    size = float(row["size"] or 0.0)
    recorded = float(row["filled_size"] or 0.0)
    venue_order = open_by_perm.get(perm_id)

    if venue_order is not None:
        _reconcile_resting(conn, row, venue_order, perm_id, size, recorded, ts, report)
        return

    con_id = _con_id_of(row, report)
    if con_id is None:
        return

    position = positions().get(con_id)
    quantity = float(position.quantity) if position is not None else 0.0

    # A venue position is the CUMULATIVE holding in that leg contract, so the
    # covering test is against the full order size (not size minus recorded
    # fills, which would double-count the part already reflected in it).
    if position is not None and quantity >= size - QTY_TOL:
        _conclude_filled(conn, row, perm_id, size, recorded, position, ts, stamp, report)
        claimed[con_id] = claimed.get(con_id, 0.0) + size
        return

    _conclude_cancelled(conn, row, perm_id, size, recorded, position, quantity,
                        ts, stamp, report)
    if recorded > QTY_TOL:
        claimed[con_id] = claimed.get(con_id, 0.0) + recorded


def _reconcile_resting(conn, row, venue_order, perm_id, size, recorded, ts, report) -> None:
    """The order is still resting at the venue: only a fill delta matters."""
    order_id = row["order_id"]
    venue_filled = float(venue_order.filled_quantity or 0.0)

    if venue_filled > recorded + QTY_TOL:
        # avg_price caveat: IB reports avgCost, which for a multiplier-1
        # ForecastEx OPT contract is ~= the per-contract price. [OPERATOR]
        # verifies this against the first real fill statement (Task 1.9).
        _append_fill_event(
            conn, perm_id=perm_id, order_id=order_id, ts=ts, cum_filled=venue_filled,
            fill_delta=venue_filled - recorded, avg_price=venue_order.avg_fill_price,
            source="sync_open_order",
        )
        conn.execute(
            "UPDATE trade_log SET status = 'partially_filled', filled_size = ?, "
            "last_synced_utc = ? WHERE order_id = ?",
            (venue_filled, ts, order_id),
        )
        report.partially_filled += 1
        if venue_filled >= size - QTY_TOL:
            report.notes.append(
                f"trade_log {order_id} (permId {perm_id}): venue reports {venue_filled:g} "
                f"of {size:g} filled but the order is still in the open list -- left "
                "'partially_filled'; the next sync closes it out"
            )
        return

    conn.execute(
        "UPDATE trade_log SET last_synced_utc = ? WHERE order_id = ?", (ts, row["order_id"])
    )
    report.unchanged += 1
    if venue_filled < recorded - QTY_TOL:
        report.notes.append(
            f"trade_log {order_id} (permId {perm_id}): venue reports {venue_filled:g} "
            f"filled but {recorded:g} are already recorded -- recorded fills kept, "
            "verify at the venue"
        )


def _conclude_filled(conn, row, perm_id, size, recorded, position, ts, stamp, report) -> None:
    """Gone from the open list and the venue holds the whole size: FILLED."""
    delta = size - recorded
    if delta > QTY_TOL:
        _append_fill_event(
            conn, perm_id=perm_id, order_id=row["order_id"], ts=ts, cum_filled=size,
            fill_delta=delta, avg_price=position.avg_price, source="sync_position_diff",
        )
    conn.execute(
        "UPDATE trade_log SET status = 'filled', filled_size = ?, last_synced_utc = ? "
        "WHERE order_id = ?",
        (size, ts, row["order_id"]),
    )
    _open_derived_position(conn, row, entry_price=float(position.avg_price), size=size,
                           stamp=stamp)
    report.filled += 1


def _conclude_cancelled(conn, row, perm_id, size, recorded, position, quantity,
                        ts, stamp, report) -> None:
    """Gone from the open list with no covering position: CANCELLED.

    Recorded partial fills are never erased -- they are venue-confirmed
    history. The partial position they represent is opened at the partial
    size, and any disagreement with the venue's position is reported.
    """
    order_id = row["order_id"]
    conn.execute(
        "UPDATE trade_log SET status = 'cancelled', last_synced_utc = ? WHERE order_id = ?",
        (ts, order_id),
    )
    report.cancelled += 1

    if recorded > QTY_TOL:
        if position is not None:
            entry_price = float(position.avg_price)
        else:
            recorded_price = _last_recorded_fill_price(conn, perm_id)
            entry_price = float(recorded_price if recorded_price is not None else row["price"])
            if recorded_price is None:
                report.notes.append(
                    f"trade_log {order_id} (permId {perm_id}): {recorded:g} contracts "
                    "recorded filled but no venue position and no priced fill_event -- "
                    "partial position opened at the order's limit price, verify it"
                )
        _open_derived_position(conn, row, entry_price=entry_price, size=recorded, stamp=stamp)

    if position is not None and abs(quantity - recorded) > QTY_TOL:
        report.notes.append(
            f"trade_log {order_id} (permId {perm_id}): venue position holds {quantity:g} "
            f"contracts in {row['market_id']} but {recorded:g} are recorded filled for "
            f"this order (ordered {size:g}) -- recorded fills kept, resolve manually"
        )
    elif position is None and recorded > QTY_TOL:
        report.notes.append(
            f"trade_log {order_id} (permId {perm_id}): {recorded:g} contracts recorded "
            f"filled but the venue reports no position in {row['market_id']} -- recorded "
            "fills kept, resolve manually"
        )


def _con_id_of(row, report) -> int | None:
    market_id = row["market_id"] or ""
    try:
        return MarketId.parse(market_id).con_id
    except (ValueError, TypeError) as exc:
        report.notes.append(
            f"trade_log {row['order_id']}: cannot read a conId from market_id "
            f"{market_id!r} ({exc}) -- skipped, never guessed"
        )
        return None


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


def _note_over_attribution(position_index, claimed: dict[int, float],
                           report: ReconcileReport) -> None:
    """Two book rows on the same leg can each be covered by one venue
    position. Attribution per order is not knowable from position data --
    say so rather than silently double-counting.
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
