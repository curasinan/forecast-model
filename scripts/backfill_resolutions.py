"""One-off backfill of ``markets.resolution`` for already-resolved markets.

Scope: markets with ``status='resolved_or_removed'``, NULL resolution, and at
least one snapshot — the only markets that can ever become training rows
(no snapshots means no features). Fetches each market's ``outcomePrices``
from Gamma's single-market endpoint and stores the normalized outcome
(1=YES, 0=NO, NULL=no definitive outcome).

Resumable by construction: already-backfilled rows have non-NULL resolution
and drop out of the candidate query, so an interrupted run just picks up
where it left off (ambiguous markets stay NULL and are re-checked — cheap at
this scope).

Usage:
    python scripts/backfill_resolutions.py                 # full backfill
    python scripts/backfill_resolutions.py --only-id 703986  # single-market sanity test
    python scripts/backfill_resolutions.py --limit 50        # bounded trial run

Writes ONLY the markets.resolution column. Read-only against everything else.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collectors.collect_markets import (  # noqa: E402
    fetch_market_resolution,
)

DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "backfill_resolutions.log"

INTER_REQUEST_DELAY_SECONDS = 0.1
COMMIT_EVERY = 100
PROGRESS_EVERY = 500

log = logging.getLogger("backfill_resolutions")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


def candidate_ids(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    """Snapshot-backed resolved markets with no stored resolution."""
    sql = """
        SELECT m.id FROM markets m
         WHERE m.status = 'resolved_or_removed'
           AND m.resolution IS NULL
           AND EXISTS (SELECT 1 FROM snapshots s WHERE s.market_id = m.id)
         ORDER BY m.id
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [str(r[0]) for r in conn.execute(sql, params).fetchall()]


def run_backfill(
    db_path: Path | str = DB_PATH,
    only_id: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    # Tolerate concurrent writers (snapshot collector runs every 15 min).
    conn.execute("PRAGMA busy_timeout = 15000")
    session = requests.Session()
    counts = {
        "processed": 0,
        "resolved_yes": 0,
        "resolved_no": 0,
        "left_null": 0,
        "errors": 0,
    }
    try:
        if only_id is not None:
            ids = [str(only_id)]
        else:
            ids = candidate_ids(conn, limit=limit)
        total = len(ids)
        log.info("Backfill scope: %s market(s)", total)

        pending_commits = 0
        for i, market_id in enumerate(ids, start=1):
            try:
                resolution = fetch_market_resolution(session, market_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Fetch failed for market %s: %s", market_id, exc)
                counts["errors"] += 1
                continue
            finally:
                counts["processed"] += 1

            if resolution == 1:
                counts["resolved_yes"] += 1
            elif resolution == 0:
                counts["resolved_no"] += 1
            else:
                counts["left_null"] += 1

            if resolution is not None:
                conn.execute(
                    "UPDATE markets SET resolution = ? WHERE id = ?",
                    (resolution, market_id),
                )
                pending_commits += 1
                if pending_commits >= COMMIT_EVERY:
                    conn.commit()
                    pending_commits = 0

            if i % PROGRESS_EVERY == 0:
                log.info(
                    "Progress: %s/%s (yes=%s no=%s null=%s errors=%s)",
                    i, total,
                    counts["resolved_yes"], counts["resolved_no"],
                    counts["left_null"], counts["errors"],
                )

            time.sleep(INTER_REQUEST_DELAY_SECONDS)

        conn.commit()
    finally:
        conn.close()

    log.info(
        "Backfill summary: processed=%s resolved_yes=%s resolved_no=%s "
        "left_null=%s errors=%s",
        counts["processed"], counts["resolved_yes"], counts["resolved_no"],
        counts["left_null"], counts["errors"],
    )
    return counts


def main() -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("scripts/backfill_resolutions.py")
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Backfill markets.resolution from Gamma outcomePrices.",
    )
    parser.add_argument(
        "--only-id", default=None,
        help="Backfill a single market id (sanity test before batch-running).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of markets processed (bounded trial run).",
    )
    args = parser.parse_args()

    log.info("Starting resolution backfill (only_id=%s, limit=%s)", args.only_id, args.limit)
    try:
        run_backfill(only_id=args.only_id, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log.critical("Backfill FAILED: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
