"""Discover live Polymarket weather markets → weather_markets table.

Sources (in order):
  1. Gamma /events?tag_slug=weather
  2. Gamma /public-search?q=highest temperature
  3. Local data/polymarket.db active markets matching temperature regex
     (uses local description/end_date; optional Gamma hydrate)

Does not touch EMOS training / backfill (Claude Code track).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from collectors.weather.parse_markets import (
    ParsedWeatherMarket,
    is_station_temperature_market,
    parse_events,
    parse_weather_market,
    upsert_parsed_markets,
)
from weather.stations import load_station_registry
from weather.storage import init_weather_db

log = logging.getLogger("discover_live_weather")

GAMMA_BASE = "https://gamma-api.polymarket.com"
REPO_ROOT = Path(__file__).resolve().parents[2]
POLYMARKET_DB = REPO_ROOT / "data" / "polymarket.db"

TEMP_Q_RE = re.compile(
    r"\b(temperature|highest temperature|lowest temperature|°[FC]|degrees?\s+[FC])\b",
    re.IGNORECASE,
)
FIXTURE_ID_RE = re.compile(r"^wx-", re.IGNORECASE)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _get_json(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    resp = client.get(f"{GAMMA_BASE}{path}", params=params or {})
    resp.raise_for_status()
    return resp.json()


def fetch_gamma_tag_events(client: httpx.Client, limit: int = 100) -> list[dict[str, Any]]:
    try:
        data = _get_json(
            client,
            "/events",
            {"active": "true", "closed": "false", "limit": min(limit, 100), "tag_slug": "weather"},
        )
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma tag_slug=weather failed: %s", exc)
        return []


def fetch_gamma_search_events(client: httpx.Client, query: str, limit: int = 40) -> list[dict[str, Any]]:
    try:
        data = _get_json(
            client,
            "/public-search",
            {"q": query, "limit_per_type": limit},
        )
        if isinstance(data, dict):
            events = data.get("events") or []
            if events:
                return events
            results = data.get("results") or {}
            if isinstance(results, dict):
                return list(results.get("events") or [])
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma public-search failed (%s): %s", query, exc)
        return []


def fetch_market_detail(client: httpx.Client, market_id: str) -> dict[str, Any] | None:
    try:
        data = _get_json(client, f"/markets/{market_id}")
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log.debug("Gamma market %s fetch failed: %s", market_id, exc)
    try:
        data = _get_json(client, "/markets", {"id": market_id})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma markets?id=%s failed: %s", market_id, exc)
    return None


def scan_local_polymarket_db(
    client: httpx.Client | None,
    db_path: Path | str = POLYMARKET_DB,
    limit: int = 2000,
    *,
    hydrate: bool = False,
    min_end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Pull temperature-ish active markets from local DB; optionally hydrate via Gamma."""
    path = Path(db_path)
    if not path.exists():
        log.info("Local polymarket.db not found at %s", path)
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT id, question, description, clob_token_ids, slug, end_date, volume
              FROM markets
             WHERE status = 'active'
             ORDER BY COALESCE(volume, 0) DESC
             LIMIT ?
            """,
            (max(limit * 8, 4000),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            """
            SELECT id, question, NULL, clob_token_ids, NULL, end_date, volume
              FROM markets
             WHERE status = 'active'
             LIMIT ?
            """,
            (max(limit * 8, 4000),),
        ).fetchall()
    finally:
        conn.close()

    min_end = min_end_date
    candidates: list[tuple] = []
    for mid, q, desc, toks, slug, end_date, _vol in rows:
        if not q or not TEMP_Q_RE.search(q):
            continue
        if not is_station_temperature_market(q, desc or ""):
            continue
        if min_end and end_date and str(end_date)[:10] < min_end:
            continue
        candidates.append((mid, q, desc, toks, slug, end_date))
        if len(candidates) >= limit:
            break

    markets: list[dict[str, Any]] = []
    for mid, question, desc, toks, slug, end_date in candidates:
        detail: dict[str, Any] = {}
        if hydrate and client is not None:
            detail = fetch_market_detail(client, str(mid)) or {}
        markets.append(
            {
                "id": str(mid),
                "question": detail.get("question") or question,
                "description": detail.get("description")
                or detail.get("rules")
                or detail.get("resolutionSource")
                or desc
                or "",
                "resolutionSource": detail.get("resolutionSource"),
                "clobTokenIds": detail.get("clobTokenIds") or toks,
                "clob_token_ids": toks,
                "slug": detail.get("slug") or slug,
                "groupItemTitle": detail.get("groupItemTitle"),
                "outcome": detail.get("outcome") or detail.get("groupItemTitle"),
                "endDate": detail.get("endDate") or end_date,
                "end_date": end_date,
            }
        )
    log.info(
        "Local DB weather-ish candidates: %s (hydrate=%s min_end=%s)",
        len(markets),
        hydrate,
        min_end,
    )
    return markets


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = str(ev.get("id") or ev.get("slug") or id(ev))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def purge_fixture_markets(db_path: Path | str | None = None) -> int:
    """Remove seed fixtures (wx-*) so paper does not mix demo IDs with live ones."""
    path = init_weather_db(db_path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("DELETE FROM weather_markets WHERE market_id LIKE 'wx-%'")
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    if n:
        log.info("Purged %s fixture weather_markets (wx-*)", n)
    return n


def discover_and_upsert(
    *,
    polymarket_db: Path | str = POLYMARKET_DB,
    weather_db: Path | str | None = None,
    timeout: float = 12.0,
    search_queries: tuple[str, ...] = (
        "highest temperature",
        "lowest temperature",
    ),
    skip_gamma: bool = False,
    local_limit: int = 2000,
    min_end_date: str | None = None,
    purge_fixtures: bool = True,
    hydrate_local: bool | None = None,
) -> dict[str, Any]:
    registry = load_station_registry()
    if min_end_date is None:
        # Keep a short lookback so recently closed books can still calibrate.
        min_end_date = (date.today() - timedelta(days=3)).isoformat()

    parsed: list[ParsedWeatherMarket] = []
    gamma_ok = False
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        events: list[dict[str, Any]] = []
        if not skip_gamma:
            events = fetch_gamma_tag_events(client)
            for q in search_queries:
                events.extend(fetch_gamma_search_events(client, q))
            events = _dedupe_events(events)
            from_events = parse_events(events, registry)
            parsed.extend(from_events)
            gamma_ok = bool(events)
            log.info("Parsed %s markets from %s Gamma events", len(from_events), len(events))
        else:
            log.info("Skipping Gamma discovery (--skip-gamma / offline)")

        do_hydrate = (not skip_gamma) if hydrate_local is None else hydrate_local
        local_markets = scan_local_polymarket_db(
            client,
            polymarket_db,
            limit=local_limit,
            hydrate=do_hydrate,
            min_end_date=min_end_date,
        )
        from_local = 0
        parse_fail = 0
        for m in local_markets:
            p = parse_weather_market(m, registry)
            if p is not None:
                parsed.append(p)
                from_local += 1
            else:
                parse_fail += 1
        log.info(
            "Parsed %s markets from local DB (%s candidates failed parse)",
            from_local,
            parse_fail,
        )

    by_id: dict[str, ParsedWeatherMarket] = {}
    for p in parsed:
        if p.market_id and not FIXTURE_ID_RE.match(p.market_id):
            by_id[p.market_id] = p
    unique = list(by_id.values())
    db_path = init_weather_db(weather_db)
    if purge_fixtures:
        purge_fixture_markets(db_path)
    n = upsert_parsed_markets(unique, db_path=db_path)
    stations = sorted({p.station_icao for p in unique})
    summary = {
        "upserted": n,
        "unique_markets": len(unique),
        "stations": stations,
        "weather_db": str(db_path),
        "gamma_ok": gamma_ok,
        "min_end_date": min_end_date,
    }
    log.info("discover summary: %s", json.dumps(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.weather.discover_live_markets")
    setup_logging()
    parser = argparse.ArgumentParser(description="Discover live Polymarket weather markets")
    parser.add_argument("--polymarket-db", type=Path, default=POLYMARKET_DB)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument(
        "--skip-gamma",
        action="store_true",
        help="Only scan local polymarket.db (no Gamma HTTP)",
    )
    parser.add_argument("--local-limit", type=int, default=2000)
    parser.add_argument(
        "--min-end-date",
        type=str,
        default=None,
        help="ISO date YYYY-MM-DD; keep markets with end_date on/after this (default: today-3d)",
    )
    parser.add_argument(
        "--keep-fixtures",
        action="store_true",
        help="Do not delete wx-* seed rows from weather_markets",
    )
    parser.add_argument(
        "--hydrate-local",
        action="store_true",
        help="Fetch Gamma market detail for local candidates (slow; needs Gamma)",
    )
    args = parser.parse_args(argv)
    discover_and_upsert(
        polymarket_db=args.polymarket_db,
        timeout=args.timeout,
        skip_gamma=args.skip_gamma,
        local_limit=args.local_limit,
        min_end_date=args.min_end_date,
        purge_fixtures=not args.keep_fixtures,
        hydrate_local=True if args.hydrate_local else (False if args.skip_gamma else None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
