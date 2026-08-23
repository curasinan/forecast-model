"""Paper weather signals + recent Polymarket edge signals."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from collectors.collect_markets import DB_PATH as POLYMARKET_DB
from mcp_server.serializers import clamp_limit, err, ok


def wx_paper_signals(
    *,
    min_edge: float = 0.05,
    require_live_quote: bool = False,
    allow_noncommercial: bool = False,
    allow_live_clob: bool = True,
    horizon_past_days: int = 0,
    horizon_future_days: int = 7,
    fetch_features: bool = True,
    paper_days: int = 0,
    polymarket_db: str | None = None,
) -> dict[str, Any]:
    """Retired: Polymarket weather paper loop. Use scripts/run_gtm_paper.py."""
    from src.retired_polymarket import retired_mcp_error

    return retired_mcp_error("wx_paper_signals")


def pm_recent_edges(
    *,
    limit: int = 50,
    tradeable_only: bool = False,
    polymarket_db: str | None = None,
    try_generate: bool = False,
) -> dict[str, Any]:
    """Recent non-weather edge/signal rows from local DB (generate path retired)."""
    path = Path(polymarket_db) if polymarket_db else Path(POLYMARKET_DB)
    lim = clamp_limit(limit)

    # Prefer persisted tables when present
    if path.exists():
        conn = sqlite3.connect(path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in ("trade_signals", "signals", "edge_signals", "paper_trades"):
                if table not in tables:
                    continue
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
                order_col = (
                    "timestamp"
                    if "timestamp" in cols
                    else ("created_at" if "created_at" in cols else cols[0])
                )
                where = ""
                if tradeable_only and "tradeable" in cols:
                    where = " WHERE tradeable = 1"
                sql = f"SELECT * FROM {table}{where} ORDER BY {order_col} DESC LIMIT ?"
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(sql, (lim,)).fetchall()]
                return ok(rows, count=len(rows), source=table, polymarket_db=str(path))
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    from src.retired_polymarket import retired_mcp_error

    if try_generate:
        return retired_mcp_error("pm_recent_edges(try_generate=true)")
    return err(
        "no_signals",
        "No persisted signal tables found; Polymarket signal generation is retired",
        polymarket_db=str(path),
    )
