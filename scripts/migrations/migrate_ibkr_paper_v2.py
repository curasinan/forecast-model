"""Migrate `ibkr_paper.db` to the v2 schema and backfill legacy permIds."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from src.trading.paper_schema import FILL_EVENTS_DDL
from src.trading.position_tracker import PositionTracker

TRADE_LOG_V2_COLUMNS = {"perm_id": "TEXT", "last_synced_utc": "TEXT"}


def _resolve_assignments(
    conn: sqlite3.Connection, permid_map
) -> tuple[dict[str, str], list[dict]]:
    """Turn a permid_map into {order_id: perm_id_str}, validated against trade_log."""
    if permid_map is None:
        return {}, []

    if isinstance(permid_map, dict):
        assignments: dict[str, str] = {}
        for order_id, perm_id in permid_map.items():
            row = conn.execute(
                "SELECT 1 FROM trade_log WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown order_id in permid map: {order_id!r}")
            assignments[order_id] = str(perm_id)
        return assignments, []

    if isinstance(permid_map, list):
        assignments = {}
        unmatched: list[dict] = []
        for item in permid_map:
            qty = item.get("qty")
            lmt = item.get("lmt")
            perm_id = item.get("perm_id")

            def _unmatched_entry() -> dict:
                return {
                    "order_id_venue": item.get("order_id_venue"),
                    "perm_id": perm_id,
                    "qty": qty,
                    "lmt": lmt,
                }

            if qty is None or lmt is None or perm_id is None:
                unmatched.append(_unmatched_entry())  # not enough info to match -- surface, never guess
                continue

            rows = conn.execute(
                "SELECT order_id FROM trade_log WHERE size = ? AND price = ?",
                (qty, lmt),
            ).fetchall()

            if not rows:
                unmatched.append(_unmatched_entry())
                continue

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
        return assignments, unmatched

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
    """Migrate `db_path` to the v2 schema and optionally backfill permIds."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.autocommit = False
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

            assignments, unmatched_entries = _resolve_assignments(conn, permid_map)
            permids_backfilled = _apply_assignments(conn, assignments)
    finally:
        conn.close()

    return {
        "columns_added": columns_added,
        "fill_events_created": fill_events_created,
        "permids_backfilled": permids_backfilled,
        "permids_unmatched": len(unmatched_entries),
        "unmatched_entries": unmatched_entries,
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

    if result["permids_unmatched"]:
        print(
            f"\nWARNING: {result['permids_unmatched']} permid-map entry(ies) did not "
            "match any trade_log row (never guessed, never applied):",
            file=sys.stderr,
        )
        for entry in result["unmatched_entries"]:
            print(
                f"  order_id_venue={entry['order_id_venue']!r} "
                f"perm_id={entry['perm_id']!r} qty={entry['qty']!r} lmt={entry['lmt']!r}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
