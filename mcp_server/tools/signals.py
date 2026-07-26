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
    """Run weather paper loop: EMOS vs market mids → edge signals summary."""
    if allow_noncommercial:
        from src.config import settings

        settings.open_meteo_allow_noncommercial = True
    try:
        from scripts.run_weather_paper import run_paper
    except Exception as exc:  # noqa: BLE001
        return err("import_failed", str(exc))
    try:
        out = run_paper(
            min_edge=min_edge,
            paper_days=paper_days,
            polymarket_db=Path(polymarket_db) if polymarket_db else POLYMARKET_DB,
            require_live_quote=require_live_quote,
            fetch_features=fetch_features,
            allow_noncommercial=allow_noncommercial,
            allow_live_clob=allow_live_clob,
            skip_fixtures=True,
            horizon_past_days=horizon_past_days,
            horizon_future_days=horizon_future_days,
        )
    except Exception as exc:  # noqa: BLE001
        return err("paper_failed", str(exc))

    signals = out.get("signals") or []
    # Keep response compact for agents
    compact = [
        {
            "market_id": s.get("market_id"),
            "station_icao": s.get("station_icao"),
            "bucket_label": s.get("bucket_label"),
            "model_p": s.get("model_p"),
            "market_mid": s.get("market_mid"),
            "edge": s.get("edge"),
            "tradeable": s.get("tradeable"),
            "quote_source": s.get("quote_source"),
            "lead_day": s.get("lead_day"),
            "reason": s.get("reason"),
        }
        for s in signals[:100]
    ]
    summary = {
        "n_signals": out.get("n_signals"),
        "tradeable": out.get("tradeable"),
        "quotes_used": out.get("quotes_used"),
        "quotes_missing": out.get("quotes_missing"),
        "features_ok": out.get("features_ok"),
        "features_missing": out.get("features_missing"),
        "skipped_stale": out.get("skipped_stale"),
        "emos_hits": out.get("emos_hits"),
        "emos_loaded": out.get("emos_loaded"),
        "feature_set": out.get("feature_set"),
        "go_live": out.get("go_live"),
    }
    return ok({"summary": summary, "signals": compact}, signal_count=len(compact))


def pm_recent_edges(
    *,
    limit: int = 50,
    tradeable_only: bool = False,
    polymarket_db: str | None = None,
    try_generate: bool = True,
) -> dict[str, Any]:
    """Recent non-weather edge/signal rows from DB, or generate_signals if available."""
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

    if not try_generate:
        return err(
            "no_signals",
            "No persisted signal tables found and try_generate=false",
            polymarket_db=str(path),
        )

    try:
        from decision.signal_generator import generate_signals
    except Exception as exc:  # noqa: BLE001
        return err("import_failed", str(exc))

    try:
        df = generate_signals(db_path=path)
    except FileNotFoundError as exc:
        return err(
            "model_missing",
            str(exc),
            hint="Politics model artifact missing; weather paper signals still work via wx_paper_signals.",
        )
    except Exception as exc:  # noqa: BLE001
        return err("generate_failed", str(exc))

    if df is None or getattr(df, "empty", True):
        return ok([], count=0, source="generate_signals")
    if tradeable_only and "tradeable" in df.columns:
        df = df[df["tradeable"]]
    records = df.head(lim).to_dict(orient="records")
    return ok(records, count=len(records), source="generate_signals")
