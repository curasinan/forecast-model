"""Refresh station temperature markets into data/polymarket.db."""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from collectors.collect_markets import DB_PATH, init_db, upsert_market
from collectors.weather.parse_markets import is_station_temperature_market

log = logging.getLogger("refresh_weather_markets")

GAMMA_BASE = "https://gamma-api.polymarket.com"
TEMP_SLUG_RE = re.compile(r"temperature|highest-temp|lowest-temp", re.I)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("markets", "data", "events"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def fetch_json(client: httpx.Client, path: str, params: dict[str, Any]) -> Any:
    resp = client.get(f"{GAMMA_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_weather_markets(timeout: float = 12.0) -> list[dict[str, Any]]:
    """Best-effort pull of active temperature markets from Gamma."""
    markets: dict[str, dict[str, Any]] = {}
    queries = (
        "highest temperature",
        "lowest temperature",
        "temperature in",
    )
    timeout_cfg = httpx.Timeout(timeout, connect=min(timeout, 8.0))
    with httpx.Client(timeout=timeout_cfg, follow_redirects=True) as client:
        # 1) public-search events (often lighter than full /events tag)
        for q in queries:
            try:
                data = fetch_json(
                    client,
                    "/public-search",
                    {"q": q, "limit_per_type": 50},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("public-search %r failed: %s", q, exc)
                continue
            events = []
            if isinstance(data, dict):
                events = list(data.get("events") or [])
                results = data.get("results") or {}
                if isinstance(results, dict) and not events:
                    events = list(results.get("events") or [])
            for ev in events:
                for m in ev.get("markets") or []:
                    if not isinstance(m, dict):
                        continue
                    if not m.get("description") and ev.get("description"):
                        m = {**m, "description": ev.get("description")}
                    mid = str(m.get("id") or "")
                    qtext = m.get("question") or ""
                    if mid and is_station_temperature_market(qtext, m.get("description") or ""):
                        markets[mid] = m
            log.info("After search %r: %s station temp markets", q, len(markets))

        for offset in (0, 100, 200, 300, 400):
            try:
                data = fetch_json(
                    client,
                    "/markets",
                    {
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("/markets offset=%s failed: %s", offset, exc)
                break
            page = _as_list(data)
            if not page:
                break
            hit = 0
            for m in page:
                mid = str(m.get("id") or "")
                qtext = m.get("question") or ""
                slug = m.get("slug") or ""
                if not mid:
                    continue
                if TEMP_SLUG_RE.search(slug) or is_station_temperature_market(
                    qtext, m.get("description") or ""
                ):
                    if is_station_temperature_market(qtext, m.get("description") or ""):
                        markets[mid] = m
                        hit += 1
            log.info("/markets offset=%s page=%s weather_hits=%s total=%s", offset, len(page), hit, len(markets))
            time.sleep(0.2)

        # 3) tag slug weather events (last; often slow)
        try:
            data = fetch_json(
                client,
                "/events",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                    "tag_slug": "weather",
                },
            )
            for ev in _as_list(data):
                for m in ev.get("markets") or []:
                    if not isinstance(m, dict):
                        continue
                    if not m.get("description") and ev.get("description"):
                        m = {**m, "description": ev.get("description")}
                    mid = str(m.get("id") or "")
                    qtext = m.get("question") or ""
                    if mid and is_station_temperature_market(qtext, m.get("description") or ""):
                        markets[mid] = m
            log.info("After tag_slug=weather: %s station temp markets", len(markets))
        except Exception as exc:  # noqa: BLE001
            log.warning("tag_slug=weather failed: %s", exc)

    return list(markets.values())


def _ensure_description_column(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    if "description" not in cols:
        try:
            conn.execute("ALTER TABLE markets ADD COLUMN description TEXT")
            return True
        except sqlite3.OperationalError:
            return False
    return True


def upsert_gamma_markets(markets: list[dict[str, Any]], db_path: Path | str = DB_PATH) -> int:
    """Insert/update weather markets, refreshing end_date / tokens / description."""
    from collectors.collect_markets import _normalize_clob_token_ids, _coerce_float

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_db(conn)
    has_desc = _ensure_description_column(conn)
    now_iso = datetime.now(timezone.utc).isoformat()
    n = 0
    try:
        for m in markets:
            market_id = m.get("id")
            if market_id is None:
                continue
            market_id = str(market_id)
            question = m.get("question")
            condition_id = m.get("conditionId")
            slug = m.get("slug")
            clob_token_ids = _normalize_clob_token_ids(m.get("clobTokenIds"))
            end_date = m.get("endDate")
            volume = _coerce_float(m.get("volume"))
            volume_24h = _coerce_float(m.get("volume24hr"))
            liquidity = _coerce_float(m.get("liquidity"))
            description = m.get("description") or m.get("rules") or ""
            exists = conn.execute(
                "SELECT 1 FROM markets WHERE id = ?", (market_id,)
            ).fetchone()
            if exists:
                if has_desc:
                    conn.execute(
                        """
                        UPDATE markets
                           SET question = COALESCE(?, question),
                               slug = COALESCE(?, slug),
                               clob_token_ids = COALESCE(?, clob_token_ids),
                               end_date = COALESCE(?, end_date),
                               volume = ?,
                               volume_24h = ?,
                               liquidity = ?,
                               description = COALESCE(?, description),
                               status = 'active',
                               last_updated = ?,
                               orderbook_missing = 0
                         WHERE id = ?
                        """,
                        (
                            question,
                            slug,
                            clob_token_ids,
                            end_date,
                            volume,
                            volume_24h,
                            liquidity,
                            description or None,
                            now_iso,
                            market_id,
                        ),
                    )
                else:
                    upsert_market(conn, m, now_iso)
            else:
                if has_desc:
                    conn.execute(
                        """
                        INSERT INTO markets (
                            id, question, condition_id, slug, clob_token_ids, end_date,
                            volume, volume_24h, liquidity, status, resolution,
                            created_at, last_updated, description
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
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
                            description or None,
                        ),
                    )
                else:
                    upsert_market(conn, m, now_iso)
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.weather.refresh_markets")
    setup_logging()
    parser = argparse.ArgumentParser(description="Refresh weather markets into polymarket.db")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)
    markets = fetch_weather_markets(timeout=args.timeout)
    if not markets:
        log.error("No weather markets fetched; Gamma may be unreachable")
        return 2
    n = upsert_gamma_markets(markets, db_path=args.db)
    # Clear orderbook_missing for refreshed ids so snapshots can retry
    conn = sqlite3.connect(args.db)
    try:
        ids = [str(m.get("id")) for m in markets if m.get("id") is not None]
        if ids:
            conn.executemany(
                "UPDATE markets SET orderbook_missing = 0, last_updated = ? WHERE id = ?",
                [(datetime.now(timezone.utc).isoformat(), i) for i in ids],
            )
            conn.commit()
    finally:
        conn.close()
    log.info("Upserted %s weather markets into %s", n, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
