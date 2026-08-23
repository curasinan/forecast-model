"""Polymarket order-book snapshot collector.

For every market returned by ``get_snapshot_candidates()`` this script pulls
the current order book and last price from the Polymarket CLOB API, computes
a handful of summary metrics, and writes one row per market to the
``snapshots`` table in ``data/polymarket.db``.

Run from the repo root:
    python -m collectors.collect_snapshots [--limit N] [--delay-ms N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collectors._lock import LockHeld, PidLock
from collectors.collect_markets import DB_PATH, get_snapshot_candidates
from src.venues.polymarket import readonly_clob_client

# --- Configuration ----------------------------------------------------------

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "snapshot_collector.log"
LOCK_PATH = REPO_ROOT / "logs" / "collect_snapshots.lock"

DEFAULT_DELAY_MS = 100
RATE_LIMIT_BACKOFF_SECONDS = 60
MAX_RATE_LIMIT_RETRIES = 3

# Stale-book detection: if the book says ~binary (bid≈0, ask≈1) but the
# price endpoint reports an interior mid, treat the book as stale.
STALE_BID_THRESHOLD = 0.02
STALE_ASK_THRESHOLD = 0.98
STALE_LAST_PRICE_MIN = 0.10
STALE_LAST_PRICE_MAX = 0.90

# Raise a critical alert if more than this share of markets errored out.
CRITICAL_FAILURE_RATIO = 0.5

log = logging.getLogger("collect_snapshots")


# --- Logging ----------------------------------------------------------------


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

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


# --- Database ---------------------------------------------------------------


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            best_bid    REAL,
            best_ask    REAL,
            bid_depth   REAL,
            ask_depth   REAL,
            midpoint    REAL,
            spread      REAL,
            last_price  REAL,
            volume_24h  REAL,
            UNIQUE (market_id, timestamp)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_market_id "
        "ON snapshots (market_id)"
    )
    conn.commit()


def _load_volume_24h_map(db_path: Path | str) -> dict[str, float | None]:
    """Pull the current volume_24h for every market in the DB."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, volume_24h FROM markets"
        ).fetchall()
    finally:
        conn.close()
    return {str(mid): v for mid, v in rows}


# --- Order-book math --------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_book_metrics(book: Any) -> dict[str, float | None]:
    """Summarize an OrderBookSummary into best/depth/mid/spread metrics.

    Polymarket does not guarantee bid/ask levels are sorted in the response,
    so we compute ``best_bid = max(bid.price)`` and ``best_ask = min(ask.price)``
    explicitly.
    """
    best_bid: float | None = None
    bid_depth: float | None = None
    bids = book.bids or []
    if bids:
        bid_depth = 0.0
        prices: list[float] = []
        for level in bids:
            p = _to_float(level.price)
            s = _to_float(level.size)
            if p is not None:
                prices.append(p)
            if s is not None:
                bid_depth += s
        if prices:
            best_bid = max(prices)

    best_ask: float | None = None
    ask_depth: float | None = None
    asks = book.asks or []
    if asks:
        ask_depth = 0.0
        prices = []
        for level in asks:
            p = _to_float(level.price)
            s = _to_float(level.size)
            if p is not None:
                prices.append(p)
            if s is not None:
                ask_depth += s
        if prices:
            best_ask = min(prices)

    midpoint: float | None = None
    spread: float | None = None
    if best_bid is not None and best_ask is not None:
        midpoint = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "midpoint": midpoint,
        "spread": spread,
    }


def is_stale_book(
    best_bid: float | None,
    best_ask: float | None,
    last_price: float | None,
) -> bool:
    """True if the book looks like the known (0.01 / 0.99) stale-data bug."""
    if best_bid is None or best_ask is None or last_price is None:
        return False
    return (
        best_bid < STALE_BID_THRESHOLD
        and best_ask > STALE_ASK_THRESHOLD
        and STALE_LAST_PRICE_MIN < last_price < STALE_LAST_PRICE_MAX
    )


# --- CLOB API helpers -------------------------------------------------------


def _is_rate_limit_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return (
        "429" in msg
        or "rate limit" in msg
        or "too many requests" in msg
    )


def _is_missing_orderbook_error(exc: BaseException) -> bool:
    """True if the CLOB returned 'no orderbook exists for this token' (404).

    Not a real error — expected for delisted/resolved/neg-risk markets.
    """
    if getattr(exc, "status_code", None) == 404:
        return True
    msg = str(exc).lower()
    return "no orderbook exists" in msg


def _call_clob(fn, *args, **kwargs):
    """Call a py-clob-client method; back off and retry on HTTP 429."""
    last_exc: BaseException | None = None
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — py-clob-client raises generic errors
            last_exc = exc
            if _is_rate_limit_error(exc):
                log.warning(
                    "Rate-limited on attempt %s/%s: %s; sleeping %ss",
                    attempt,
                    MAX_RATE_LIMIT_RETRIES,
                    exc,
                    RATE_LIMIT_BACKOFF_SECONDS,
                )
                if attempt < MAX_RATE_LIMIT_RETRIES:
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                continue
            raise
    raise RuntimeError(
        f"Rate-limit retries exhausted after {MAX_RATE_LIMIT_RETRIES} attempts: "
        f"{last_exc}"
    )


def _first_token_id(clob_token_ids: str | None) -> str | None:
    if not clob_token_ids:
        return None
    try:
        tokens = json.loads(clob_token_ids)
    except json.JSONDecodeError:
        return None
    if not isinstance(tokens, list) or not tokens:
        return None
    return str(tokens[0])


# --- Main collection loop ---------------------------------------------------


def collect_snapshots(
    db_path: Path | str = DB_PATH,
    delay_ms: int = DEFAULT_DELAY_MS,
    limit: int | None = None,
) -> dict[str, int | float]:
    """Snapshot every candidate market's order book into the snapshots table.

    Returns a summary dict with counts of stored / stale / errored markets.
    """
    client = readonly_clob_client(CLOB_HOST, chain_id=POLYGON_CHAIN_ID)

    candidates = get_snapshot_candidates(db_path)
    if limit is not None:
        candidates = candidates[:limit]

    total = len(candidates)
    log.info(
        "Starting snapshot run: %s candidates, delay=%sms",
        total,
        delay_ms,
    )

    volume_24h_map = _load_volume_24h_map(db_path)
    delay_s = max(delay_ms, 0) / 1000.0

    started = time.time()
    rows_to_insert: list[tuple] = []
    missing_orderbook_ids: list[str] = []
    stale_count = 0
    error_count = 0
    missing_book_count = 0
    empty_side_count = 0

    for idx, market in enumerate(candidates, start=1):
        market_id = str(market["id"])
        token_id = _first_token_id(market.get("clob_token_ids"))
        if token_id is None:
            log.warning("Market %s has no usable token id; skipping", market_id)
            error_count += 1
            continue

        try:
            book = _call_clob(client.get_order_book, token_id)
            if delay_s:
                time.sleep(delay_s)
            price_resp = _call_clob(client.get_price, token_id, "BUY")
            if delay_s:
                time.sleep(delay_s)
        except Exception as exc:  # noqa: BLE001
            if _is_missing_orderbook_error(exc):
                # Expected for delisted / resolved / neg-risk markets —
                # flag the row so future runs skip it until collect_markets
                # re-sees the market and clears the flag.
                log.info("Market %s: no orderbook on CLOB; flagging as missing", market_id)
                missing_orderbook_ids.append(market_id)
                missing_book_count += 1
            else:
                log.error("Market %s: CLOB API error: %s", market_id, exc)
                error_count += 1
            continue

        try:
            metrics = extract_book_metrics(book)
            last_price = _to_float((price_resp or {}).get("price"))
        except Exception as exc:  # noqa: BLE001
            log.error("Market %s: failed parsing book: %s", market_id, exc)
            error_count += 1
            continue

        if metrics["best_bid"] is None:
            log.warning("Market %s: empty bid side", market_id)
            empty_side_count += 1
        if metrics["best_ask"] is None:
            log.warning("Market %s: empty ask side", market_id)
            empty_side_count += 1

        if is_stale_book(metrics["best_bid"], metrics["best_ask"], last_price):
            log.warning(
                "Market %s: stale book (bid=%.4f, ask=%.4f, last=%.4f); skipping",
                market_id,
                metrics["best_bid"],
                metrics["best_ask"],
                last_price,
            )
            stale_count += 1
            continue

        timestamp = datetime.now(timezone.utc).isoformat()
        rows_to_insert.append(
            (
                market_id,
                timestamp,
                metrics["best_bid"],
                metrics["best_ask"],
                metrics["bid_depth"],
                metrics["ask_depth"],
                metrics["midpoint"],
                metrics["spread"],
                last_price,
                volume_24h_map.get(market_id),
            )
        )

        if idx % 100 == 0:
            log.info(
                "Progress: %s/%s (stored=%s, stale=%s, missing_book=%s, errors=%s)",
                idx,
                total,
                len(rows_to_insert),
                stale_count,
                missing_book_count,
                error_count,
            )

    # --- Batch write in a single transaction -------------------------------
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        if rows_to_insert:
            conn.executemany(
                """
                INSERT OR IGNORE INTO snapshots (
                    market_id, timestamp, best_bid, best_ask,
                    bid_depth, ask_depth, midpoint, spread,
                    last_price, volume_24h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
        if missing_orderbook_ids:
            conn.executemany(
                "UPDATE markets SET orderbook_missing = 1 WHERE id = ?",
                [(mid,) for mid in missing_orderbook_ids],
            )
        conn.commit()
    finally:
        conn.close()

    elapsed = time.time() - started
    stored = len(rows_to_insert)
    summary: dict[str, int | float] = {
        "total_candidates": total,
        "stored": stored,
        "stale_skipped": stale_count,
        "missing_book_skipped": missing_book_count,
        "error_skipped": error_count,
        "empty_side_warnings": empty_side_count,
        "elapsed_seconds": round(elapsed, 1),
    }

    log.info(
        "Run summary: total=%s stored=%s stale=%s missing_book=%s errors=%s elapsed=%.1fs",
        total,
        stored,
        stale_count,
        missing_book_count,
        error_count,
        elapsed,
    )

    if total > 0 and error_count / total > CRITICAL_FAILURE_RATIO:
        log.critical(
            "CRITICAL: %s/%s markets failed (%.1f%%) — CLOB API may be unhealthy",
            error_count,
            total,
            100 * error_count / total,
        )

    return summary


# --- CLI entry point --------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Snapshot the Polymarket CLOB order book for candidate markets.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only snapshot the first N candidates (useful for testing).",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=DEFAULT_DELAY_MS,
        help=f"Delay between CLOB API calls in ms (default: {DEFAULT_DELAY_MS}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.collect_snapshots")
    setup_logging()
    args = _parse_args(argv)
    try:
        with PidLock(LOCK_PATH):
            log.info("Starting Polymarket snapshot collection run")
            try:
                summary = collect_snapshots(
                    delay_ms=args.delay_ms,
                    limit=args.limit,
                )
            except Exception as exc:  # noqa: BLE001
                log.critical(
                    "Unhandled error during snapshot run: %s", exc, exc_info=True
                )
                return 1

            total = summary.get("total_candidates", 0)
            errors = summary.get("error_skipped", 0)
            if isinstance(total, int) and total > 0 and isinstance(errors, int):
                if errors / total > CRITICAL_FAILURE_RATIO:
                    return 1
            return 0
    except LockHeld as exc:
        log.info(
            "Previous collect_snapshots run still active (pid=%s); skipping this cycle",
            exc.pid,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
