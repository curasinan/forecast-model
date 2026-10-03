"""MCP tool handlers with tmp DBs (no live network)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from mcp_server.tools import markets, quotes, status
from src.config import settings
from weather.storage import init_weather_db


@pytest.fixture
def poly_db(tmp_path: Path) -> Path:
    db = tmp_path / "polymarket.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            description TEXT,
            clob_token_ids TEXT,
            slug TEXT,
            end_date TEXT,
            volume REAL,
            liquidity REAL,
            status TEXT,
            orderbook_missing INTEGER DEFAULT 0
        );
        CREATE TABLE snapshots (
            market_id TEXT,
            timestamp TEXT,
            midpoint REAL,
            spread REAL,
            best_bid REAL,
            best_ask REAL,
            bid_depth REAL,
            ask_depth REAL,
            last_price REAL,
            volume_24h REAL
        );
        """
    )
    future = (date.today() + timedelta(days=2)).isoformat() + "T12:00:00Z"
    conn.execute(
        """
        INSERT INTO markets
        (id, question, description, clob_token_ids, slug, end_date, volume, liquidity, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            "m-london-1",
            f"Will the highest temperature in London be 24°C on {date.today().strftime('%B')} {date.today().day}?",
            "",
            '["tok1"]',
            "london-temp",
            future,
            1000.0,
            2000.0,
        ),
    )
    # Simpler question that parse_valid_date + city hint always handle
    conn.execute(
        """
        INSERT INTO markets
        (id, question, description, clob_token_ids, slug, end_date, volume, liquidity, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            "m-london-2",
            "Will the highest temperature in London be 25°C on July 28?",
            "",
            '["tok2"]',
            "london-temp-2",
            "2026-07-28T12:00:00Z",
            500.0,
            1500.0,
        ),
    )
    conn.execute(
        """
        INSERT INTO snapshots
        (market_id, timestamp, midpoint, spread, best_bid, best_ask, bid_depth, ask_depth, last_price, volume_24h)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "m-london-2",
            "2026-07-26T10:00:00+00:00",
            0.42,
            0.04,
            0.40,
            0.44,
            10,
            10,
            0.42,
            1.0,
        ),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def weather_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wxroot"
    root.mkdir()
    # resolve_weather_data_root() reads the ``settings`` singleton, built once
    # at import -- a WEATHER_DATA_ROOT set in the environment now never
    # reaches it, and the default would be the REAL ~/quant_data/polymarket_weather.
    # Patch the attribute, so init_weather_db() and the default-path resource
    # handlers below all land in this tmp root.
    monkeypatch.setattr(settings, "weather_data_root", str(root))
    return init_weather_db()


def test_status_counts(poly_db: Path, weather_db: Path):
    out = status.status(
        ping_network=False,
        polymarket_db=str(poly_db),
        weather_db=str(weather_db),
    )
    assert out["ok"] is True
    assert out["data"]["polymarket"]["markets"] == 2
    assert out["data"]["polymarket"]["snapshots"] == 1
    assert out["data"]["weather"]["exists"] is True


def test_wx_discover_and_list(poly_db: Path, weather_db: Path):
    disc = markets.wx_discover_markets(
        skip_gamma=True,
        min_end_date="2026-07-01",
        polymarket_db=str(poly_db),
        weather_db=str(weather_db),
    )
    assert disc["ok"] is True
    assert disc["data"]["unique_markets"] >= 1

    listed = markets.wx_list_markets(weather_db=str(weather_db), limit=20)
    assert listed["ok"] is True
    assert listed["meta"]["count"] >= 1
    assert any(r["station_icao"] == "EGLC" for r in listed["data"])


def test_wx_get_quote_from_snapshot(poly_db: Path):
    q = quotes.wx_get_quote(
        "m-london-2",
        allow_live=False,
        polymarket_db=str(poly_db),
    )
    assert q["ok"] is True
    assert q["data"]["midpoint"] == 0.42
    assert q["data"]["source"] == "polymarket_snapshot"


def test_pm_latest_snapshots(poly_db: Path):
    out = quotes.pm_latest_snapshots(polymarket_db=str(poly_db), limit=10)
    assert out["ok"] is True
    assert out["meta"]["count"] == 1
    assert out["data"][0]["market_id"] == "m-london-2"


def test_pm_list_markets(poly_db: Path):
    out = markets.pm_list_markets(query="London", polymarket_db=str(poly_db), limit=10)
    assert out["ok"] is True
    assert out["meta"]["count"] >= 1


def test_server_registers_tools():
    from mcp_server.server import mcp

    # FastMCP keeps tools on _tool_manager
    names = set(mcp._tool_manager._tools.keys())  # noqa: SLF001
    expected = {
        "status",
        "wx_list_markets",
        "wx_discover_markets",
        "wx_refresh_gamma",
        "wx_get_quote",
        "wx_snapshot_books",
        "wx_collect_forecasts",
        "wx_collect_obs",
        "wx_paper_signals",
        "pm_list_markets",
        "pm_latest_snapshots",
        "pm_recent_edges",
    }
    assert expected.issubset(names)


def test_resources_json(poly_db: Path, weather_db: Path):
    from mcp_server.server import resource_weather_status

    # Patch default DB paths by calling status helpers directly already covered;
    # resources use default paths — just ensure they return parseable JSON.
    raw = resource_weather_status()
    data = json.loads(raw)
    assert "path" in data or "exists" in data
