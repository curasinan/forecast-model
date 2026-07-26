"""Live-quote wiring + discover helpers (no live Gamma required)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from collectors.weather.discover_live_markets import (
    _dedupe_events,
    purge_fixture_markets,
    scan_local_polymarket_db,
)
from collectors.weather.parse_markets import upsert_parsed_markets, parse_weather_market
from weather.market_quotes import latest_quote, resolve_quote
from weather.stations import load_station_registry
from weather.storage import init_weather_db


def test_dedupe_events():
    events = [
        {"id": "1", "slug": "a"},
        {"id": "1", "slug": "a"},
        {"id": "2", "slug": "b"},
    ]
    assert len(_dedupe_events(events)) == 2


def test_latest_quote(tmp_path: Path):
    db = tmp_path / "poly.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE snapshots (
            market_id TEXT, timestamp TEXT, midpoint REAL, spread REAL,
            best_bid REAL, best_ask REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
        [
            ("m1", "2026-07-25T10:00:00+00:00", 0.40, 0.04, 0.38, 0.42),
            ("m1", "2026-07-25T11:00:00+00:00", 0.55, 0.02, 0.54, 0.56),
        ],
    )
    conn.commit()
    conn.close()
    q = latest_quote("m1", db_path=db)
    assert q is not None
    assert q.midpoint == 0.55
    assert q.spread == 0.02
    assert q.usable
    assert latest_quote("missing", db_path=db) is None


def test_resolve_quote_prefers_snapshot(tmp_path: Path):
    db = tmp_path / "poly.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE snapshots (
            market_id TEXT, timestamp TEXT, midpoint REAL, spread REAL,
            best_bid REAL, best_ask REAL
        )
        """
    )
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, clob_token_ids TEXT)"
    )
    conn.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
        ("m1", "2026-07-25T11:00:00+00:00", 0.42, 0.03, 0.40, 0.43),
    )
    conn.commit()
    conn.close()
    q = resolve_quote("m1", db_path=db, allow_live=False)
    assert q is not None and q.midpoint == 0.42
    assert q.source == "polymarket_snapshot"


def test_scan_local_uses_end_date_and_parses(tmp_path: Path):
    poly = tmp_path / "poly.db"
    conn = sqlite3.connect(poly)
    conn.execute(
        """
        CREATE TABLE markets (
            id TEXT, question TEXT, description TEXT, clob_token_ids TEXT,
            slug TEXT, end_date TEXT, volume REAL, status TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            "555",
            "Will the highest temperature in London be 24°C on July 28?",
            "",
            '["tok"]',
            "london-temp",
            "2026-07-28T12:00:00Z",
            1000.0,
            "active",
        ),
    )
    conn.commit()
    conn.close()
    rows = scan_local_polymarket_db(
        None, poly, limit=10, hydrate=False, min_end_date="2026-07-25"
    )
    assert len(rows) == 1
    p = parse_weather_market(rows[0], load_station_registry())
    assert p is not None
    assert p.market_id == "555"
    assert p.valid_date == "2026-07-28"


def test_purge_fixtures(tmp_path: Path):
    wdb = tmp_path / "weather.db"
    init_weather_db(wdb)
    registry = load_station_registry()
    live = parse_weather_market(
        {
            "id": "999",
            "question": "Will the highest temperature in London be 24°C on July 28?",
            "description": "",
            "endDate": "2026-07-28T12:00:00Z",
        },
        registry,
    )
    fixture = parse_weather_market(
        {
            "id": "wx-nyc-1",
            "question": "Highest temperature in NYC on July 28?",
            "groupItemTitle": "84-85°F",
            "description": (
                "LaGuardia https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA "
                "28 Jul '26"
            ),
        },
        registry,
    )
    assert live and fixture
    upsert_parsed_markets([live, fixture], db_path=wdb)
    n = purge_fixture_markets(wdb)
    assert n == 1
    conn = sqlite3.connect(wdb)
    ids = [r[0] for r in conn.execute("SELECT market_id FROM weather_markets")]
    conn.close()
    assert ids == ["999"]
