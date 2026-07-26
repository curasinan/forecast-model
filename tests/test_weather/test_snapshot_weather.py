"""Weather liquidity override for get_snapshot_candidates."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collectors.collect_markets import get_snapshot_candidates


def _init_markets(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            clob_token_ids TEXT,
            end_date TEXT,
            liquidity REAL,
            volume REAL,
            status TEXT,
            orderbook_missing INTEGER
        )
        """
    )
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    conn.executemany(
        """
        INSERT INTO markets
        (id, question, clob_token_ids, end_date, liquidity, volume, status, orderbook_missing)
        VALUES (?, ?, ?, ?, ?, ?, 'active', 0)
        """,
        [
            # Weather: low liquidity — should still qualify under override
            (
                "w1",
                "Highest temperature in NYC on July 28?",
                '["tok"]',
                future,
                5000,
                10000,
            ),
            # Politics: low liquidity — should NOT qualify
            (
                "p1",
                "Will candidate X win the election?",
                '["tok"]',
                far,
                5000,
                10000,
            ),
            # Politics: high liquidity — should qualify
            (
                "p2",
                "Will candidate Y win the election?",
                '["tok"]',
                far,
                300000,
                600000,
            ),
        ],
    )
    conn.commit()
    conn.close()


def test_weather_liquidity_override(tmp_path: Path):
    db = tmp_path / "m.db"
    _init_markets(db)
    cands = get_snapshot_candidates(db)
    ids = {c["id"] for c in cands}
    assert "w1" in ids
    assert "p1" not in ids
    assert "p2" in ids
