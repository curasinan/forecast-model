"""SQLite-backed cache of resolved ForecastEx contract specifications.

Cache invalidation policy:
  - Every row carries ``fetched_at``; rows older than 24h (CACHE_TTL_HOURS)
    are stale. ``resolve()`` re-fetches stale/missing rows from the API.
  - ``invalidate(con_id)`` evicts one row; ``flush_all()`` clears the table
    and should be called on reconnect events (listings can change while the
    session is down).
  - When the API rejects a cached conId, the caller evicts it (``invalidate``)
    and re-resolves ONCE via ``resolve``; if the fresh row is rejected again
    the error propagates -- never retry in a loop.
  - Seeded rows (``seed_from_snapshot``) count as fresh from their snapshot
    load time, so a freshly seeded universe is usable without a connection.

Never construct a Contract from guessed field values (spec Stage 3.2):
requests go out by conId only, and rows come exclusively from
reqContractDetails responses or the verified universe snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.venues.base import VenueError
from src.venues.ibkr.settings import IBKRSettings

CACHE_TTL_HOURS = 24

_DDL = """
CREATE TABLE IF NOT EXISTS contracts (
    con_id INTEGER PRIMARY KEY,
    symbol TEXT,
    sec_type TEXT,
    expiry TEXT,
    strike REAL,
    "right" TEXT,
    local_symbol TEXT,
    trading_class TEXT,
    multiplier TEXT,
    min_tick REAL,
    currency TEXT,
    exchange TEXT,
    long_name TEXT,
    fetched_at TEXT
)
"""

_COLUMNS = (
    "con_id", "symbol", "sec_type", "expiry", "strike", "right", "local_symbol",
    "trading_class", "multiplier", "min_tick", "currency", "exchange", "long_name",
    "fetched_at",
)

_UPSERT = (
    'INSERT OR REPLACE INTO contracts (con_id, symbol, sec_type, expiry, strike, "right", '
    "local_symbol, trading_class, multiplier, min_tick, currency, exchange, long_name, "
    "fetched_at) VALUES (" + ", ".join("?" for _ in _COLUMNS) + ")"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContractResolver:
    """conId -> contract-spec cache backed by resolved_data_root/contracts_cache.db."""

    def __init__(self, settings: IBKRSettings):
        root = settings.resolved_data_root
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "contracts_cache.db"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- cache population --------------------------------------------------
    def seed_from_snapshot(self, path: str | Path) -> int:
        """Load a universe snapshot (data/ibkr_universe_live.json shape).

        Maps the snapshot's ib_async field names onto cache columns; rows
        without a conId are skipped. Returns the number of rows seeded.
        Seeded rows are stamped fresh as of the load time.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        now = _utcnow().isoformat()
        seeded = 0
        for raw in data.get("contracts", {}).values():
            con_id = raw.get("conId")
            if not con_id:
                continue
            self._upsert({
                "con_id": int(con_id),
                "symbol": raw.get("symbol") or "",
                "sec_type": raw.get("secType") or "",
                "expiry": raw.get("lastTradeDateOrContractMonth") or "",
                "strike": raw.get("strike"),
                "right": raw.get("right") or "",
                "local_symbol": raw.get("localSymbol") or "",
                "trading_class": raw.get("tradingClass") or "",
                "multiplier": raw.get("multiplier") or "",
                "min_tick": raw.get("minTick"),
                "currency": raw.get("currency") or "",
                "exchange": raw.get("exchange") or "",
                "long_name": raw.get("longName") or "",
                "fetched_at": now,
            })
            seeded += 1
        self._conn.commit()
        return seeded

    # -- reads -------------------------------------------------------------
    def get(self, con_id: int) -> dict | None:
        """Cached row for con_id (fresh or stale), or None on a miss."""
        row = self._conn.execute(
            "SELECT * FROM contracts WHERE con_id = ?", (int(con_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def tradable_con_ids(self) -> list[int]:
        """Fresh, unexpired, tradable (non-IND) conIds in the cache."""
        today = _utcnow().strftime("%Y%m%d")
        ids: list[int] = []
        rows = self._conn.execute(
            "SELECT con_id, expiry, fetched_at FROM contracts WHERE sec_type != 'IND'"
        )
        for row in rows:
            if not self._is_fresh(row["fetched_at"]):
                continue
            expiry = (row["expiry"] or "")[:8]
            if expiry and expiry < today:
                continue
            ids.append(row["con_id"])
        return sorted(ids)

    def yes_no_pair(self, symbol: str, expiry: str, strike: float) -> tuple[int, int]:
        """(yes_con_id, no_con_id) for a market, from cached rows.

        ForecastEx lists YES and NO as separate conIds: right='C' is YES,
        right='P' is NO (verified 2026-08-16). Raises VenueError when the
        cache does not hold both legs.
        """
        rows = self._conn.execute(
            'SELECT con_id, "right" FROM contracts '
            "WHERE symbol = ? AND expiry = ? AND strike = ? AND sec_type != 'IND'",
            (symbol, expiry, float(strike)),
        ).fetchall()
        by_right = {row["right"]: row["con_id"] for row in rows}
        if "C" not in by_right or "P" not in by_right:
            raise VenueError(
                f"no cached YES/NO pair for ({symbol!r}, {expiry!r}, {strike!r}); "
                f"rights present: {sorted(by_right)}"
            )
        return by_right["C"], by_right["P"]

    # -- resolution --------------------------------------------------------
    def resolve(self, ib, con_id: int) -> dict:
        """Return the row for con_id, fetching via reqContractDetails on miss/stale."""
        row = self.get(con_id)
        if row is not None and self._is_fresh(row["fetched_at"]):
            return row
        from ib_async import Contract  # lazy: cache-only use needs no ib_async

        details = ib.reqContractDetails(Contract(conId=int(con_id), exchange="FORECASTX"))
        if not details:
            raise VenueError(
                f"conId {con_id} did not resolve on FORECASTX; if it was cached, it has "
                "been dropped by the venue (invalidate() has no row left to serve)"
            )
        detail = details[0]
        c = detail.contract
        fresh = {
            "con_id": int(c.conId),
            "symbol": getattr(c, "symbol", "") or "",
            "sec_type": getattr(c, "secType", "") or "",
            "expiry": getattr(c, "lastTradeDateOrContractMonth", "") or "",
            "strike": getattr(c, "strike", None),
            "right": getattr(c, "right", "") or "",
            "local_symbol": getattr(c, "localSymbol", "") or "",
            "trading_class": getattr(c, "tradingClass", "") or "",
            "multiplier": getattr(c, "multiplier", "") or "",
            "min_tick": getattr(detail, "minTick", None),
            "currency": getattr(c, "currency", "") or "",
            "exchange": getattr(c, "exchange", "") or "FORECASTX",
            "long_name": getattr(detail, "longName", "") or "",
            "fetched_at": _utcnow().isoformat(),
        }
        self._upsert(fresh)
        self._conn.commit()
        return fresh

    # -- invalidation ------------------------------------------------------
    def invalidate(self, con_id: int) -> None:
        """Evict one row (call before the single re-resolve after an API rejection)."""
        self._conn.execute("DELETE FROM contracts WHERE con_id = ?", (int(con_id),))
        self._conn.commit()

    def flush_all(self) -> None:
        """Clear the cache; call on reconnect events (listings may have changed)."""
        self._conn.execute("DELETE FROM contracts")
        self._conn.commit()

    # -- internals ---------------------------------------------------------
    def _upsert(self, row: dict) -> None:
        self._conn.execute(_UPSERT, tuple(row[col] for col in _COLUMNS))

    @staticmethod
    def _is_fresh(fetched_at: str | None) -> bool:
        if not fetched_at:
            return False
        try:
            fetched = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return _utcnow() - fetched <= timedelta(hours=CACHE_TTL_HOURS)
