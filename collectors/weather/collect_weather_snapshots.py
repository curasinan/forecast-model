"""Snapshot CLOB books for markets already in weather_markets.

Bypasses politics liquidity filters so thin daily temperature buckets get mids.
Requires network access to clob.polymarket.com.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from collectors.collect_markets import DB_PATH
from collectors.collect_snapshots import (
    CLOB_HOST,
    POLYGON_CHAIN_ID,
    _call_clob,
    _first_token_id,
    _is_missing_orderbook_error,
    _load_volume_24h_map,
    _to_float,
    extract_book_metrics,
    init_db,
    is_stale_book,
)
from src.venues.polymarket import readonly_clob_client
from weather.storage import init_weather_db

log = logging.getLogger("collect_weather_snapshots")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def weather_snapshot_candidates(
    weather_db: Path | str | None = None,
    polymarket_db: Path | str = DB_PATH,
) -> list[dict]:
    wdb = init_weather_db(weather_db)
    conn_w = sqlite3.connect(wdb)
    try:
        rows = conn_w.execute(
            """
            SELECT market_id, clob_token_ids, question
              FROM weather_markets
             WHERE market_id NOT LIKE 'wx-%'
            """
        ).fetchall()
    finally:
        conn_w.close()

    conn = sqlite3.connect(polymarket_db)
    out: list[dict] = []
    try:
        for mid, toks, question in rows:
            mid = str(mid)
            row = conn.execute(
                "SELECT clob_token_ids, question FROM markets WHERE id = ?",
                (mid,),
            ).fetchone()
            token_raw = (row[0] if row and row[0] else None) or toks
            q = (row[1] if row and row[1] else None) or question
            if _first_token_id(token_raw) is None:
                continue
            out.append({"id": mid, "question": q, "clob_token_ids": token_raw})
    finally:
        conn.close()
    return out


def collect_weather_snapshots(
    *,
    polymarket_db: Path | str = DB_PATH,
    weather_db: Path | str | None = None,
    delay_ms: int = 100,
    limit: int | None = None,
) -> dict[str, int]:
    client = readonly_clob_client(CLOB_HOST, chain_id=POLYGON_CHAIN_ID)
    candidates = weather_snapshot_candidates(weather_db, polymarket_db)
    if limit is not None:
        candidates = candidates[:limit]
    log.info("Weather snapshot candidates: %s", len(candidates))

    volume_24h_map = _load_volume_24h_map(polymarket_db)
    delay_s = max(delay_ms, 0) / 1000.0
    rows_to_insert: list[tuple] = []
    missing = 0
    errors = 0
    stale = 0

    for market in candidates:
        market_id = str(market["id"])
        token_id = _first_token_id(market.get("clob_token_ids"))
        if token_id is None:
            errors += 1
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
                missing += 1
            else:
                log.warning("CLOB error %s: %s", market_id, exc)
                errors += 1
            continue

        try:
            metrics = extract_book_metrics(book)
            last_price = _to_float((price_resp or {}).get("price"))
        except Exception as exc:  # noqa: BLE001
            log.warning("parse error %s: %s", market_id, exc)
            errors += 1
            continue

        if is_stale_book(metrics["best_bid"], metrics["best_ask"], last_price):
            stale += 1
            continue

        rows_to_insert.append(
            (
                market_id,
                datetime.now(timezone.utc).isoformat(),
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

    conn = sqlite3.connect(polymarket_db)
    try:
        init_db(conn)
        conn.executemany(
            """
            INSERT OR IGNORE INTO snapshots (
                market_id, timestamp, best_bid, best_ask, bid_depth, ask_depth,
                midpoint, spread, last_price, volume_24h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        conn.commit()
    finally:
        conn.close()

    summary = {
        "candidates": len(candidates),
        "stored": len(rows_to_insert),
        "missing_book": missing,
        "stale": stale,
        "errors": errors,
    }
    log.info("weather snapshots: %s", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.weather.collect_weather_snapshots")
    setup_logging()
    parser = argparse.ArgumentParser(description="Snapshot weather_markets CLOB books")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay-ms", type=int, default=100)
    parser.add_argument("--polymarket-db", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)
    try:
        collect_weather_snapshots(
            polymarket_db=args.polymarket_db,
            delay_ms=args.delay_ms,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Weather snapshot collect failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
