"""Health / connectivity / DB counts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx

from collectors.collect_markets import DB_PATH as POLYMARKET_DB
from mcp_server.serializers import err, ok
from weather.paths import resolve_weather_data_root
from weather.storage import init_weather_db, weather_db_path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return None


def _ping(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 4.0))) as client:
            resp = client.get(url)
            return {"reachable": True, "status_code": resp.status_code}
    except httpx.TimeoutException as exc:
        return {"reachable": False, "error": "timeout", "detail": str(exc)}
    except httpx.HTTPError as exc:
        msg = str(exc)
        code = "ssl_error" if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper() else "http_error"
        return {"reachable": False, "error": code, "detail": msg}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": type(exc).__name__, "detail": str(exc)}


def polymarket_status(db_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(db_path) if db_path else Path(POLYMARKET_DB)
    if not path.exists():
        return {"path": str(path), "exists": False}
    conn = sqlite3.connect(path)
    try:
        markets = _table_count(conn, "markets")
        snaps = _table_count(conn, "snapshots")
        latest = None
        try:
            latest = conn.execute("SELECT MAX(timestamp) FROM snapshots").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        active = None
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM markets WHERE status = 'active'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return {
        "path": str(path),
        "exists": True,
        "markets": markets,
        "active_markets": active,
        "snapshots": snaps,
        "latest_snapshot_ts": latest,
    }


def weather_status(db_path: Path | str | None = None) -> dict[str, Any]:
    root = resolve_weather_data_root()
    path = Path(db_path) if db_path else weather_db_path(root)
    emos_dir = root / "models" / "emos"
    emos_n = len(list(emos_dir.glob("*.json"))) if emos_dir.exists() else 0
    if not path.exists():
        return {
            "path": str(path),
            "data_root": str(root),
            "exists": False,
            "emos_models": emos_n,
        }
    # Ensure schema present for count queries
    init_weather_db(path)
    conn = sqlite3.connect(path)
    try:
        counts = {
            t: _table_count(conn, t)
            for t in (
                "weather_markets",
                "forecast_daily",
                "forecast_history_daily",
                "obs_daily",
                "resolution_labels",
                "model_probabilities",
            )
        }
    finally:
        conn.close()
    return {
        "path": str(path),
        "data_root": str(root),
        "exists": True,
        "counts": counts,
        "emos_models": emos_n,
    }


def status(
    *,
    ping_network: bool = True,
    polymarket_db: str | None = None,
    weather_db: str | None = None,
) -> dict[str, Any]:
    """DB paths, table counts, and optional Gamma/CLOB connectivity ping."""
    poly = polymarket_status(polymarket_db)
    wx = weather_status(weather_db)
    net: dict[str, Any] = {}
    if ping_network:
        net["gamma"] = _ping(f"{GAMMA_BASE}/markets", timeout=5.0)
        net["clob"] = _ping(f"{CLOB_HOST}/", timeout=5.0)
        if not net["gamma"].get("reachable"):
            net["hint"] = (
                "Gamma unreachable (timeout/SSL). Use wx_discover_markets(skip_gamma=true) "
                "against local polymarket.db."
            )
    return ok({"polymarket": poly, "weather": wx, "network": net})
