"""Quotes and weather CLOB snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from collectors.collect_markets import DB_PATH as POLYMARKET_DB
from mcp_server.serializers import clamp_limit, err, ok
from weather.market_quotes import resolve_quote


def wx_get_quote(
    market_id: str,
    *,
    allow_live: bool = True,
    clob_token_ids: str | None = None,
    polymarket_db: str | None = None,
) -> dict[str, Any]:
    """Resolve mid/spread for a weather market (snapshot, then live CLOB)."""
    if not market_id:
        return err("bad_request", "market_id is required")
    db = Path(polymarket_db) if polymarket_db else Path(POLYMARKET_DB)
    try:
        q = resolve_quote(
            market_id,
            clob_token_ids=clob_token_ids,
            db_path=db,
            allow_live=allow_live,
        )
    except Exception as exc:  # noqa: BLE001
        return err("quote_failed", str(exc), market_id=market_id)
    if q is None:
        return err(
            "quote_missing",
            f"No snapshot/live quote for {market_id}",
            market_id=market_id,
            hint="Run wx_snapshot_books or ensure CLOB is reachable.",
        )
    return ok(
        {
            "market_id": q.market_id,
            "midpoint": q.midpoint,
            "spread": q.spread,
            "best_bid": q.best_bid,
            "best_ask": q.best_ask,
            "timestamp": q.timestamp,
            "source": q.source,
            "usable": q.usable,
        }
    )


def wx_snapshot_books(
    *,
    limit: int | None = 100,
    delay_ms: int = 100,
    polymarket_db: str | None = None,
    weather_db: str | None = None,
) -> dict[str, Any]:
    """Snapshot CLOB books for markets in weather_markets."""
    try:
        from collectors.weather.collect_weather_snapshots import collect_weather_snapshots
    except Exception as exc:  # noqa: BLE001
        return err("import_failed", str(exc))
    try:
        summary = collect_weather_snapshots(
            polymarket_db=Path(polymarket_db) if polymarket_db else POLYMARKET_DB,
            weather_db=weather_db,
            delay_ms=delay_ms,
            limit=limit,
        )
        return ok(summary)
    except Exception as exc:  # noqa: BLE001
        return err(
            "clob_unreachable",
            str(exc),
            hint="CLOB may be blocked (timeout/SSL). Quotes need network or existing snapshots.",
        )


def pm_latest_snapshots(
    *,
    market_ids: list[str] | None = None,
    limit: int = 50,
    polymarket_db: str | None = None,
) -> dict[str, Any]:
    """Latest snapshot mid/spread per market (recent books from polymarket.db)."""
    path = Path(polymarket_db) if polymarket_db else Path(POLYMARKET_DB)
    if not path.exists():
        return err("db_missing", f"polymarket.db not found: {path}")
    lim = clamp_limit(limit)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if market_ids:
            ids = [str(m) for m in market_ids][:lim]
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"""
                SELECT s.market_id, s.midpoint, s.spread, s.best_bid, s.best_ask, s.timestamp
                  FROM snapshots s
                  INNER JOIN (
                    SELECT market_id, MAX(timestamp) AS ts
                      FROM snapshots
                     WHERE market_id IN ({placeholders})
                     GROUP BY market_id
                  ) latest ON latest.market_id = s.market_id AND latest.ts = s.timestamp
                """,
                ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.market_id, s.midpoint, s.spread, s.best_bid, s.best_ask, s.timestamp
                  FROM snapshots s
                  INNER JOIN (
                    SELECT market_id, MAX(timestamp) AS ts
                      FROM snapshots
                     GROUP BY market_id
                     ORDER BY ts DESC
                     LIMIT ?
                  ) latest ON latest.market_id = s.market_id AND latest.ts = s.timestamp
                 ORDER BY s.timestamp DESC
                """,
                (lim,),
            ).fetchall()
        data = [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        return err("query_failed", str(exc))
    finally:
        conn.close()
    return ok(data, count=len(data), polymarket_db=str(path))
