"""Migrate `ibkr_paper.db` to the v2 schema and backfill legacy permIds.

Three `placed` rows written on 2026-08-25 predate the `perm_id` column
added to `trade_log` by Task 1.3, so they have no venue permId on file.
The operator captured those permIds out-of-band via `reqAllOpenOrders`
into a JSON file. This script (idempotent, safe to re-run):

1. Ensures `trade_log` has the `perm_id`/`last_synced_utc` columns (a
   no-op if Task 1.3's `PositionTracker._ensure_columns` already added
   them -- this migration reuses that exact static method).
2. Creates the `fill_events` table (see `src.trading.paper_schema` for
   the DDL and the single source of truth for this table's shape).
3. Backfills `perm_id` for rows named in an operator-supplied permId map,
   accepting either shape:
   - ``{order_id: perm_id}`` -- a simple mapping, matched by order_id.
     An order_id absent from `trade_log` raises `ValueError` naming it.
   - the raw Task-0.2 `reqAllOpenOrders` capture: a list of dicts with
     keys ``order_id_venue``, ``perm_id``, ``con_id``, ``local_symbol``,
     ``action``, ``qty``, ``lmt``, ``status``. Venue order ids do not
     appear in `trade_log`, so these are matched to a row by
     ``qty == trade_log.size AND lmt == trade_log.price``. A match that
     is not unique (multiple trade_log rows with the same size+price, or
     conflicting perm_ids resolving to the same row) raises `ValueError`
     rather than guessing.

`positions` is never touched -- the v2 schema needs no changes there.

All schema changes and backfill writes happen in a single transaction:
`sqlite3.Connection.autocommit` is set to `False` (Python 3.12+) so that
DDL (CREATE/ALTER, which the stdlib sqlite3 module would otherwise
auto-commit ahead of a surrounding transaction) rolls back together with
the backfill UPDATEs if anything raises partway through.

Timestamp/commission semantics for the `fill_events` table this script
creates (operator-approved 2026-08-25; full note lives in
`src.trading.paper_schema`, the single source of truth):

- `detected_ts_utc` is the sync **detection** time, always set. On the
  position-diff path the TRUE fill time is unknown -- a fill that landed
  days ago is only detected at the next sync. Neither `detected_ts_utc`
  nor `ts_utc` may be treated as the fill time in P&L timing analysis.
- `ts_utc` is the best-known event time: for sync-created rows today it
  equals `detected_ts_utc`; a future execution-report enrichment may set
  it to the venue execution time instead.
- `commission_usd` stays NULL today; the column exists now so a future
  `reqExecutions`/`CommissionReport` enrichment does not need a second
  migration.

CLI:

    python scripts/migrations/migrate_ibkr_paper_v2.py --db <path> \\
        [--permid-map <path-to-json>]

`--permid-map` is optional -- omitting it runs a schema-only migration.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from src.trading.paper_schema import FILL_EVENTS_DDL
from src.trading.position_tracker import PositionTracker

# Columns Task 1.3 added to trade_log; this migration must be a no-op on
# a db where PositionTracker has already added them.
TRADE_LOG_V2_COLUMNS = {"perm_id": "TEXT", "last_synced_utc": "TEXT"}


def _resolve_assignments(conn: sqlite3.Connection, permid_map) -> dict[str, str]:
    """Turn a permid_map (either accepted shape) into {order_id: perm_id_str},
    validating every entry against the current trade_log contents.
    """
    if permid_map is None:
        return {}

    if isinstance(permid_map, dict):
        assignments: dict[str, str] = {}
        for order_id, perm_id in permid_map.items():
            row = conn.execute(
                "SELECT 1 FROM trade_log WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown order_id in permid map: {order_id!r}")
            assignments[order_id] = str(perm_id)
        return assignments

    if isinstance(permid_map, list):
        assignments = {}
        for item in permid_map:
            qty = item.get("qty")
            lmt = item.get("lmt")
            perm_id = item.get("perm_id")
            if qty is None or lmt is None or perm_id is None:
                continue  # not enough info to match a row -- skip, never guess

            rows = conn.execute(
                "SELECT order_id FROM trade_log WHERE size = ? AND price = ?",
                (qty, lmt),
            ).fetchall()

            if not rows:
                continue  # no matching legacy row -- fine, capture may cover more than the target rows

            if len(rows) > 1:
                raise ValueError(
                    f"Ambiguous permid match: qty={qty} lmt={lmt} matches "
                    f"{len(rows)} trade_log rows ({[r[0] for r in rows]}) -- refusing to guess"
                )

            order_id = rows[0][0]
            perm_id_str = str(perm_id)
            if order_id in assignments and assignments[order_id] != perm_id_str:
                raise ValueError(
                    f"Ambiguous permid match: qty={qty} lmt={lmt} resolves to trade_log "
                    f"row {order_id!r} with conflicting perm_ids "
                    f"{assignments[order_id]!r} and {perm_id_str!r}"
                )
            assignments[order_id] = perm_id_str
        return assignments

    raise TypeError(f"permid_map must be a dict, list, or None; got {type(permid_map).__name__}")


def _apply_assignments(conn: sqlite3.Connection, assignments: dict[str, str]) -> int:
    """Write perm_id for each assignment, returning how many rows actually changed."""
    backfilled = 0
    for order_id, perm_id in assignments.items():
        row = conn.execute(
            "SELECT perm_id FROM trade_log WHERE order_id = ?", (order_id,)
        ).fetchone()
        current = row[0] if row else None
        if current == perm_id:
            continue  # already backfilled -- idempotent no-op
        conn.execute(
            "UPDATE trade_log SET perm_id = ? WHERE order_id = ?", (perm_id, order_id)
        )
        backfilled += 1
    return backfilled


def migrate(db_path: str, permid_map: dict | list | None = None) -> dict:
    """Migrate `db_path` to the v2 schema and optionally backfill permIds.

    Returns
        {"columns_added": [...], "fill_events_created": bool, "permids_backfilled": int}

    Idempotent: re-running with the same (or no) permid_map changes nothing.
    All writes happen in a single transaction (see module docstring).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.autocommit = False  # make DDL participate in the same transaction as the DML below
        with conn:
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
            columns_added = [c for c in TRADE_LOG_V2_COLUMNS if c not in existing_cols]
            PositionTracker._ensure_columns(conn, "trade_log", TRADE_LOG_V2_COLUMNS)

            table_existed = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fill_events'"
                ).fetchone()
                is not None
            )
            conn.execute(FILL_EVENTS_DDL)
            fill_events_created = not table_existed

            assignments = _resolve_assignments(conn, permid_map)
            permids_backfilled = _apply_assignments(conn, assignments)
    finally:
        conn.close()

    return {
        "columns_added": columns_added,
        "fill_events_created": fill_events_created,
        "permids_backfilled": permids_backfilled,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate ibkr_paper.db to the v2 schema (fill_events table + permId backfill)."
    )
    parser.add_argument("--db", required=True, help="Path to the ibkr_paper.db SQLite file")
    parser.add_argument(
        "--permid-map",
        default=None,
        help=(
            "Path to a JSON permId map: either {order_id: perm_id} or the "
            "operator's reqAllOpenOrders capture list. Omit for a schema-only migration."
        ),
    )
    args = parser.parse_args(argv)

    permid_map = None
    if args.permid_map:
        permid_map = json.loads(Path(args.permid_map).read_text(encoding="utf-8"))

    try:
        result = migrate(args.db, permid_map)
    except (ValueError, TypeError) as exc:
        print(f"Migration aborted: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
