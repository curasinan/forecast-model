"""Polymarket market data collector.

Fetches active events (and their nested markets) from the Gamma API and
upserts them into a local SQLite database. Markets previously marked
'active' but not seen in this run are flagged 'resolved_or_removed'.

Run from the repo root:
    python -m collectors.collect_markets
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from collectors._lock import LockHeld, PidLock
from src.utils.dates import parse_iso8601

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
EVENTS_ENDPOINT = f"{GAMMA_BASE_URL}/events"
MARKETS_ENDPOINT = f"{GAMMA_BASE_URL}/markets"
PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 30

# Max Gamma single-market lookups per collector run for the resolution pass.
# The collector fires every 6h -> up to 400 resolutions/day, well ahead of the
# observed resolution rate, while keeping each run fast.
RESOLUTION_PASS_LIMIT = 100


class PaginationLimitReached(Exception):
    """Raised when the Gamma API signals the offset pagination ceiling (HTTP 422).

    As of 2026-06 the Gamma /events endpoint rejects offset >= 2100 with a 422.
    This is a normal end-of-pagination signal, not a transient error — the
    caller stops paginating cleanly rather than retrying.
    """

    def __init__(self, offset: int):
        self.offset = offset
        super().__init__(f"pagination limit reached at offset={offset}")

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "market_collector.log"

log = logging.getLogger("collect_markets")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    # Avoid duplicate handlers if setup_logging() is called twice.
    for handler in list(log.handlers):
        log.removeHandler(handler)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS markets (
            id                TEXT PRIMARY KEY,
            question          TEXT,
            condition_id      TEXT,
            slug              TEXT,
            clob_token_ids    TEXT,
            end_date          TEXT,
            volume            REAL,
            volume_24h        REAL,
            liquidity         REAL,
            status            TEXT NOT NULL DEFAULT 'active',
            resolution        TEXT,
            created_at        TEXT,
            last_updated      TEXT,
            orderbook_missing INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Additive migration for databases created before orderbook_missing existed.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    if "orderbook_missing" not in existing_columns:
        conn.execute(
            "ALTER TABLE markets "
            "ADD COLUMN orderbook_missing INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def fetch_events_page(offset: int) -> list[dict[str, Any]]:
    """Fetch a single page of events, retrying transient failures.

    Raises RuntimeError if all retries are exhausted so the caller can
    decide whether the failure should abort the run.
    """
    params = {
        "closed": "false",
        "order": "id",
        "ascending": "false",
        "limit": PAGE_SIZE,
        "offset": offset,
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                EVENTS_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status = response.status_code
            if status == 200:
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError(
                        f"Expected JSON array from /events, got {type(payload).__name__}"
                    )
                return payload
            if status == 422:
                # Gamma's offset-pagination ceiling. Not retryable, not an
                # error — signal clean end-of-pagination to the caller.
                raise PaginationLimitReached(offset)
            if status == 429 or 500 <= status < 600:
                log.warning(
                    "Gamma /events returned %s on attempt %s/%s (offset=%s); "
                    "retrying in %ss",
                    status,
                    attempt,
                    MAX_RETRIES,
                    offset,
                    RETRY_DELAY_SECONDS,
                )
                last_error = RuntimeError(f"HTTP {status}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue
            # Non-retryable HTTP error.
            response.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.warning(
                "Gamma /events request failed on attempt %s/%s (offset=%s): %s",
                attempt,
                MAX_RETRIES,
                offset,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to fetch /events offset={offset} after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def iter_markets(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for event in events:
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            log.warning(
                "Event %s has non-list markets field; skipping",
                event.get("id"),
            )
            continue
        for market in markets:
            if isinstance(market, dict):
                yield market


def _normalize_clob_token_ids(value: Any) -> str | None:
    """Gamma returns clobTokenIds as either a list or a JSON-encoded string.

    Normalize to a JSON array string for storage.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        return json.dumps(parsed)
    if isinstance(value, list):
        return json.dumps(value)
    return json.dumps(value)


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_market(
    conn: sqlite3.Connection,
    market: dict[str, Any],
    now_iso: str,
) -> str:
    """Insert or update a market row. Returns 'inserted' or 'updated'."""
    market_id = market.get("id")
    if market_id is None:
        raise ValueError("market missing 'id' field")
    market_id = str(market_id)

    question = market.get("question")
    condition_id = market.get("conditionId")
    slug = market.get("slug")
    clob_token_ids = _normalize_clob_token_ids(market.get("clobTokenIds"))
    end_date = market.get("endDate")
    volume = _coerce_float(market.get("volume"))
    volume_24h = _coerce_float(market.get("volume24hr"))
    liquidity = _coerce_float(market.get("liquidity"))

    cursor = conn.execute("SELECT 1 FROM markets WHERE id = ?", (market_id,))
    exists = cursor.fetchone() is not None

    if exists:
        conn.execute(
            """
            UPDATE markets
               SET volume            = ?,
                   volume_24h        = ?,
                   liquidity         = ?,
                   status            = 'active',
                   last_updated      = ?,
                   orderbook_missing = 0
             WHERE id = ?
            """,
            (volume, volume_24h, liquidity, now_iso, market_id),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO markets (
            id, question, condition_id, slug, clob_token_ids, end_date,
            volume, volume_24h, liquidity, status, resolution,
            created_at, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
        """,
        (
            market_id,
            question,
            condition_id,
            slug,
            clob_token_ids,
            end_date,
            volume,
            volume_24h,
            liquidity,
            now_iso,
            now_iso,
        ),
    )
    return "inserted"


def mark_stale(
    conn: sqlite3.Connection,
    seen_ids: set[str],
    now_iso: str,
) -> int:
    cursor = conn.execute("SELECT id FROM markets WHERE status = 'active'")
    active_ids = {row[0] for row in cursor.fetchall()}
    stale_ids = active_ids - seen_ids
    if not stale_ids:
        return 0

    conn.executemany(
        """
        UPDATE markets
           SET status = 'resolved_or_removed',
               last_updated = ?
         WHERE id = ?
        """,
        [(now_iso, mid) for mid in stale_ids],
    )
    return len(stale_ids)


# --------------------------------------------------------------------------- #
# Resolution capture (Gamma outcomePrices -> markets.resolution)
# --------------------------------------------------------------------------- #


def _parse_outcome_prices(raw: Any) -> int | None:
    """Normalize Gamma's ``outcomePrices`` field to a binary resolution.

    Gamma returns it as a JSON-encoded string like ``'["0", "1"]'`` (YES price
    first), but is handled defensively as a real list too — the API has
    changed encodings before (cf. ``clobTokenIds``). A YES price pinned to 1
    means resolved YES, pinned to 0 means resolved NO. Anything interior
    (e.g. ``["0.56", "0.44"]`` on a removed/unresolved market) is not a
    definitive outcome -> None. Never raises.
    """
    if raw is None:
        return None
    prices = raw
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    try:
        yes_price = float(prices[0])
    except (TypeError, ValueError):
        return None
    if yes_price >= 0.999:
        return 1
    if yes_price <= 0.001:
        return 0
    return None


def fetch_market_resolution(
    session: requests.Session,
    market_id: str,
) -> int | None:
    """Fetch one market from Gamma and return its resolution (1/0) or None.

    None means: not found (404), not closed yet, no definitive outcome, or
    retries exhausted on transient errors. Callers leave the DB row NULL in
    all of those cases.
    """
    url = f"{MARKETS_ENDPOINT}/{market_id}"
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            status = response.status_code
            if status == 200:
                market = response.json()
                if not isinstance(market, dict):
                    return None
                if not market.get("closed"):
                    # Not actually closed on Gamma's side; no trustworthy outcome.
                    return None
                return _parse_outcome_prices(market.get("outcomePrices"))
            if status == 404:
                return None
            if status == 429 or 500 <= status < 600:
                log.warning(
                    "Gamma /markets/%s returned %s on attempt %s/%s; retrying in %ss",
                    market_id, status, attempt, MAX_RETRIES, RETRY_DELAY_SECONDS,
                )
                last_error = RuntimeError(f"HTTP {status}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue
            log.warning("Gamma /markets/%s returned unexpected HTTP %s", market_id, status)
            return None
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.warning(
                "Gamma /markets/%s request failed on attempt %s/%s: %s",
                market_id, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    log.error(
        "Gamma /markets/%s: retries exhausted (%s); leaving resolution NULL",
        market_id, last_error,
    )
    return None


def fetch_missing_resolutions(
    conn: sqlite3.Connection,
    session: requests.Session | None = None,
    limit: int = RESOLUTION_PASS_LIMIT,
    fetcher=fetch_market_resolution,
) -> int:
    """Populate ``markets.resolution`` for snapshot-backed resolved markets.

    Picks up to ``limit`` markets with status='resolved_or_removed', NULL
    resolution, and at least one snapshot (only snapshot-backed markets can
    ever become training rows), newest last_updated first so freshly-flipped
    markets are captured promptly. Returns the number of rows updated.
    Per-market failures are logged and skipped; this never aborts the run.
    """
    rows = conn.execute(
        """
        SELECT m.id FROM markets m
         WHERE m.status = 'resolved_or_removed'
           AND m.resolution IS NULL
           AND EXISTS (SELECT 1 FROM snapshots s WHERE s.market_id = m.id)
         ORDER BY m.last_updated DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return 0

    if session is None:
        session = requests.Session()

    updated = 0
    for (market_id,) in rows:
        market_id = str(market_id)
        try:
            resolution = fetcher(session, market_id)
        except Exception as exc:  # noqa: BLE001 — one bad market must not kill the run
            log.error("Resolution fetch failed for market %s: %s", market_id, exc)
            continue
        if resolution is None:
            continue
        conn.execute(
            "UPDATE markets SET resolution = ? WHERE id = ?",
            (resolution, market_id),
        )
        updated += 1
    conn.commit()
    return updated


# Backwards-compatible alias: this helper used to live in this module before
# being promoted to ``src.utils.dates``. Kept so any old import paths still work.
_parse_iso8601 = parse_iso8601


def get_snapshot_candidates(
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    """Return markets eligible for order-book snapshotting.

    Filter:
      - status = 'active'
      - clob_token_ids is non-empty (not NULL, '', or '[]')
      - liquidity > 250000 OR volume > 500000
      - end_date is more than 24 hours in the future
      - orderbook_missing is not set (i.e. the CLOB has a book for it;
        the flag is cleared on every collect_markets run so a relisted
        market becomes eligible again automatically)

    Each returned dict has keys: ``id``, ``question``, ``clob_token_ids``.
    Also logs the count of candidates vs total active markets.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, question, clob_token_ids, end_date
              FROM markets
             WHERE status = 'active'
               AND clob_token_ids IS NOT NULL
               AND clob_token_ids != ''
               AND clob_token_ids != '[]'
               AND (liquidity > 250000 OR volume > 500000)
               AND end_date IS NOT NULL
               AND end_date != ''
               AND (orderbook_missing = 0 OR orderbook_missing IS NULL)
            """
        ).fetchall()
        total_active = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE status = 'active'"
        ).fetchone()[0]
    finally:
        conn.close()

    cutoff = datetime.now(timezone.utc) + timedelta(hours=24)
    candidates: list[dict[str, Any]] = []
    for market_id, question, clob_token_ids, end_date in rows:
        end_dt = _parse_iso8601(end_date)
        if end_dt is None or end_dt <= cutoff:
            continue
        candidates.append(
            {
                "id": market_id,
                "question": question,
                "clob_token_ids": clob_token_ids,
            }
        )

    pct = 100 * len(candidates) / total_active if total_active else 0.0
    log.info(
        "Snapshot candidates: %s / %s active markets (%.2f%%)",
        len(candidates),
        total_active,
        pct,
    )
    return candidates


def run() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        # Wait up to 15s on a locked DB instead of failing instantly — the
        # snapshot collector (and occasional backfill scripts) write to the
        # same file on their own schedules.
        conn.execute("PRAGMA busy_timeout = 15000")
        init_db(conn)

        now_iso = datetime.now(timezone.utc).isoformat()
        seen_ids: set[str] = set()
        inserted = 0
        updated = 0
        processed = 0
        offset = 0
        page = 0
        pagination_truncated = False

        while True:
            page += 1
            try:
                events = fetch_events_page(offset)
            except PaginationLimitReached as exc:
                log.info(
                    "Reached pagination limit (HTTP 422 at offset %s) — stopping "
                    "pagination and proceeding to stale sweep.", exc.offset,
                )
                pagination_truncated = True
                break
            except RuntimeError as exc:
                log.critical(
                    "Gamma API unreachable; aborting run: %s", exc
                )
                return 1

            if not events:
                log.info("No events returned at offset=%s; pagination complete", offset)
                break

            log.info(
                "Page %s: fetched %s events (offset=%s)",
                page,
                len(events),
                offset,
            )

            for market in iter_markets(events):
                try:
                    market_id = market.get("id")
                    if market_id is None:
                        log.warning("Skipping market with no id: %s", market)
                        continue
                    market_id = str(market_id)

                    result = upsert_market(conn, market, now_iso)
                    seen_ids.add(market_id)
                    processed += 1
                    if result == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:
                    log.error(
                        "Failed to process market id=%s: %s",
                        market.get("id"),
                        exc,
                    )
                    continue

            conn.commit()

            if len(events) < PAGE_SIZE:
                log.info(
                    "Page %s returned fewer than %s events; pagination complete",
                    page,
                    PAGE_SIZE,
                )
                break
            offset += PAGE_SIZE

        if pagination_truncated:
            # The API truncated pagination at its offset ceiling, so seen_ids
            # is only the newest slice of the active universe (not the whole
            # set). Running mark_stale here would wrongly flip every active
            # market beyond the ceiling to resolved_or_removed. Skip it — a
            # resolved market lingering as 'active' is caught downstream by the
            # signal generator's freshness guard and the snapshot 404 handler,
            # which is far safer than mass-mislabeling live markets.
            stale_count = 0
            log.warning(
                "Stale sweep SKIPPED — pagination was truncated by the API "
                "(only newest %s markets seen). Resolved-market detection "
                "deferred to downstream guards.",
                len(seen_ids),
            )
        else:
            stale_count = mark_stale(conn, seen_ids, now_iso)
        conn.commit()

        # Resolution pass: capture Gamma outcomePrices for snapshot-backed
        # markets that resolved (flipped by this or any earlier run) but have
        # no stored outcome yet. Bounded per run to keep the collector fast.
        try:
            resolutions_captured = fetch_missing_resolutions(conn)
        except Exception as exc:  # noqa: BLE001 — never let this kill the run
            log.error("Resolution pass failed: %s", exc)
            resolutions_captured = 0
        if resolutions_captured:
            log.info("Resolution pass: captured %s outcome(s)", resolutions_captured)

        log.info(
            "Run summary: processed=%s, new=%s, updated=%s, marked_stale=%s%s, "
            "resolutions_captured=%s",
            processed,
            inserted,
            updated,
            stale_count,
            " (stale sweep skipped: pagination truncated)" if pagination_truncated else "",
            resolutions_captured,
        )
        return 0
    finally:
        conn.close()


LOCK_PATH = REPO_ROOT / "logs" / "collect_markets.lock"


def main() -> int:
    setup_logging()
    try:
        with PidLock(LOCK_PATH):
            log.info("Starting Polymarket market collection run")
            try:
                exit_code = run()
            except Exception as exc:
                log.critical(
                    "Unhandled error during collection run: %s", exc, exc_info=True
                )
                return 1
            log.info("Collection run finished with exit code %s", exit_code)
            return exit_code
    except LockHeld as exc:
        log.info(
            "Previous collect_markets run still active (pid=%s); skipping this cycle",
            exc.pid,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
