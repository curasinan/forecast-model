"""Weather + Polymarket market discovery / listing."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from collectors.collect_markets import DB_PATH as POLYMARKET_DB
from collectors.collect_markets import get_snapshot_candidates
from collectors.weather.discover_live_markets import discover_and_upsert
from collectors.weather.refresh_markets import fetch_weather_markets, upsert_gamma_markets
from mcp_server.serializers import clamp_limit, err, ok
from weather.storage import init_weather_db


def wx_list_markets(
    *,
    station_icao: str | None = None,
    min_valid_date: str | None = None,
    limit: int = 50,
    weather_db: str | None = None,
) -> dict[str, Any]:
    """List station temperature markets from weather_markets."""
    path = init_weather_db(weather_db)
    lim = clamp_limit(limit)
    sql = """
        SELECT market_id, question, station_icao, valid_local_date, metric, unit,
               bucket_lo, bucket_hi, bucket_label, clob_token_ids
          FROM weather_markets
         WHERE 1=1
    """
    params: list[Any] = []
    if station_icao:
        sql += " AND station_icao = ?"
        params.append(station_icao.upper())
    if min_valid_date:
        sql += " AND valid_local_date >= ?"
        params.append(min_valid_date)
    sql += " ORDER BY valid_local_date DESC, station_icao LIMIT ?"
    params.append(lim)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    return ok(rows, count=len(rows), weather_db=str(path))


def wx_discover_markets(
    *,
    skip_gamma: bool = True,
    min_end_date: str | None = None,
    local_limit: int = 2000,
    purge_fixtures: bool = True,
    polymarket_db: str | None = None,
    weather_db: str | None = None,
) -> dict[str, Any]:
    """Refresh weather_markets from local polymarket.db (and optionally Gamma)."""
    try:
        summary = discover_and_upsert(
            polymarket_db=Path(polymarket_db) if polymarket_db else POLYMARKET_DB,
            weather_db=weather_db,
            skip_gamma=skip_gamma,
            local_limit=local_limit,
            min_end_date=min_end_date,
            purge_fixtures=purge_fixtures,
        )
        return ok(summary)
    except Exception as exc:  # noqa: BLE001
        return err("discover_failed", str(exc))


def wx_refresh_gamma(
    *,
    timeout: float = 12.0,
    polymarket_db: str | None = None,
) -> dict[str, Any]:
    """Pull live station-temp markets from Gamma into polymarket.db."""
    try:
        markets = fetch_weather_markets(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return err(
            "gamma_unreachable",
            str(exc),
            hint="Use wx_discover_markets(skip_gamma=true) against local DB.",
        )
    if not markets:
        return err(
            "gamma_unreachable",
            "No weather markets fetched; Gamma may be unreachable",
            hint="Use wx_discover_markets(skip_gamma=true) against local DB.",
        )
    db = Path(polymarket_db) if polymarket_db else Path(POLYMARKET_DB)
    try:
        n = upsert_gamma_markets(markets, db_path=db)
    except Exception as exc:  # noqa: BLE001
        return err("upsert_failed", str(exc), fetched=len(markets))
    return ok({"upserted": n, "fetched": len(markets), "polymarket_db": str(db)})


def pm_list_markets(
    *,
    query: str | None = None,
    snapshot_candidates_only: bool = False,
    limit: int = 50,
    polymarket_db: str | None = None,
) -> dict[str, Any]:
    """List recent active markets from polymarket.db."""
    path = Path(polymarket_db) if polymarket_db else Path(POLYMARKET_DB)
    lim = clamp_limit(limit)
    if not path.exists():
        return err("db_missing", f"polymarket.db not found: {path}")

    if snapshot_candidates_only:
        cands = get_snapshot_candidates(path)[:lim]
        if query:
            q = query.lower()
            cands = [c for c in cands if q in (c.get("question") or "").lower()]
        return ok(cands, count=len(cands), source="snapshot_candidates")

    sql = """
        SELECT id, question, status, end_date, volume, liquidity, slug
          FROM markets
         WHERE status = 'active'
    """
    params: list[Any] = []
    if query:
        sql += " AND lower(question) LIKE ?"
        params.append(f"%{query.lower()}%")
    sql += " ORDER BY COALESCE(volume, 0) DESC LIMIT ?"
    params.append(lim)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    return ok(rows, count=len(rows), polymarket_db=str(path))
