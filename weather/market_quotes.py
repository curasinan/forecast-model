"""Load CLOB mid/spread for weather markets (snapshot DB, then live fallback)."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.venues.polymarket import readonly_clob_client

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLYMARKET_DB = REPO_ROOT / "data" / "polymarket.db"

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketQuote:
    market_id: str
    midpoint: float | None
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    timestamp: str | None
    source: str = "snapshot"

    @property
    def usable(self) -> bool:
        return self.midpoint is not None and 0.0 < self.midpoint < 1.0


def _first_token_id(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return str(raw[0]) if raw else None
    text = str(raw).strip()
    if not text or text == "[]":
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and parsed:
            return str(parsed[0])
    except json.JSONDecodeError:
        pass
    # bare token id
    if text.isdigit():
        return text
    return None


def latest_quote(
    market_id: str,
    db_path: Path | str = DEFAULT_POLYMARKET_DB,
) -> MarketQuote | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            """
            SELECT midpoint, spread, best_bid, best_ask, timestamp
              FROM snapshots
             WHERE market_id = ?
             ORDER BY timestamp DESC
             LIMIT 1
            """,
            (str(market_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    mid, spread, bid, ask, ts = row
    return MarketQuote(
        market_id=str(market_id),
        midpoint=float(mid) if mid is not None else None,
        spread=float(spread) if spread is not None else None,
        best_bid=float(bid) if bid is not None else None,
        best_ask=float(ask) if ask is not None else None,
        timestamp=ts,
        source="polymarket_snapshot",
    )


def clob_token_ids_for_market(
    market_id: str,
    db_path: Path | str = DEFAULT_POLYMARKET_DB,
) -> str | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT clob_token_ids FROM markets WHERE id = ?",
            (str(market_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def live_clob_quote(
    market_id: str,
    clob_token_ids: Any = None,
    *,
    polymarket_db: Path | str = DEFAULT_POLYMARKET_DB,
) -> MarketQuote | None:
    """Fetch YES-token mid from CLOB when snapshots are missing/stale."""
    token_id = _first_token_id(clob_token_ids)
    if token_id is None:
        token_id = _first_token_id(clob_token_ids_for_market(market_id, polymarket_db))
    if token_id is None:
        return None
    try:
        client = readonly_clob_client(CLOB_HOST, chain_id=POLYGON_CHAIN_ID)
    except ImportError:
        log.debug("py_clob_client not installed; live quote unavailable")
        return None

    try:
        book = client.get_order_book(token_id)
        price_resp = client.get_price(token_id, "BUY")
    except Exception as exc:  # noqa: BLE001
        log.debug("Live CLOB quote failed for %s: %s", market_id, exc)
        return None

    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    bid_prices = [float(b.price) for b in bids if getattr(b, "price", None) is not None]
    ask_prices = [float(a.price) for a in asks if getattr(a, "price", None) is not None]
    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    mid = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
    elif isinstance(price_resp, dict) and price_resp.get("price") is not None:
        try:
            mid = float(price_resp["price"])
        except (TypeError, ValueError):
            mid = None

    from datetime import datetime, timezone

    return MarketQuote(
        market_id=str(market_id),
        midpoint=mid,
        spread=spread,
        best_bid=best_bid,
        best_ask=best_ask,
        timestamp=datetime.now(timezone.utc).isoformat(),
        source="clob_live",
    )


def resolve_quote(
    market_id: str,
    *,
    clob_token_ids: Any = None,
    db_path: Path | str = DEFAULT_POLYMARKET_DB,
    allow_live: bool = True,
) -> MarketQuote | None:
    """Prefer latest snapshot; optionally fall back to live CLOB book."""
    q = latest_quote(market_id, db_path=db_path)
    if q is not None and q.usable:
        return q
    if not allow_live:
        return q
    live = live_clob_quote(market_id, clob_token_ids, polymarket_db=db_path)
    if live is not None and live.usable:
        return live
    return q if q is not None else live


def quotes_for_markets(
    market_ids: list[str],
    db_path: Path | str = DEFAULT_POLYMARKET_DB,
) -> dict[str, MarketQuote]:
    out: dict[str, MarketQuote] = {}
    for mid in market_ids:
        q = latest_quote(mid, db_path=db_path)
        if q is not None:
            out[mid] = q
    return out
