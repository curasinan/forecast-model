"""Tests for the resolution-capture helpers in collectors.collect_markets.

Synthetic-only — no live Gamma calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from collectors.collect_markets import (
    _parse_outcome_prices,
    fetch_market_resolution,
    fetch_missing_resolutions,
)


# --------------------------------------------------------------------------- #
# _parse_outcome_prices
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [
    ('["0", "1"]', 0),          # YES price 0 -> resolved NO
    ('["1", "0"]', 1),          # YES price 1 -> resolved YES
    ('["0.56", "0.44"]', None), # interior prices -> no definitive outcome
    (["1", "0"], 1),            # real-list form (defensive)
    (["0", "1"], 0),
    ('["0.9995", "0.0005"]', 1),   # within pin tolerance
    ('["0.0005", "0.9995"]', 0),
    ('["0.99", "0.01"]', None),    # close but not pinned
    (None, None),
    ("garbage", None),
    ('["1"]', None),               # wrong arity
    ('[]', None),
    (42, None),
    ('["abc", "def"]', None),      # non-numeric
])
def test_parse_outcome_prices(raw, expected):
    assert _parse_outcome_prices(raw) == expected


# --------------------------------------------------------------------------- #
# fetch_market_resolution (canned-response session)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return self._response


def test_fetch_resolution_closed_resolved_no():
    session = _FakeSession(_FakeResponse(200, {
        "id": "703986", "closed": True, "outcomePrices": '["0", "1"]',
    }))
    assert fetch_market_resolution(session, "703986") == 0


def test_fetch_resolution_closed_resolved_yes():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": True, "outcomePrices": '["1", "0"]',
    }))
    assert fetch_market_resolution(session, "x") == 1


def test_fetch_resolution_not_closed_returns_none():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": False, "outcomePrices": '["1", "0"]',
    }))
    assert fetch_market_resolution(session, "x") is None


def test_fetch_resolution_404_returns_none():
    session = _FakeSession(_FakeResponse(404))
    assert fetch_market_resolution(session, "gone") is None
    assert session.calls == 1  # no retries on 404


def test_fetch_resolution_ambiguous_prices_returns_none():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": True, "outcomePrices": '["0.56", "0.44"]',
    }))
    assert fetch_market_resolution(session, "x") is None


# --------------------------------------------------------------------------- #
# fetch_missing_resolutions (synthetic DB + stubbed fetcher)
# --------------------------------------------------------------------------- #


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "synth.db")
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, status TEXT, "
        "resolution TEXT, last_updated TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, market_id TEXT)"
    )
    rows = [
        # id, status, resolution, last_updated, has_snapshot
        ("m1", "resolved_or_removed", None, "2026-06-01", True),   # candidate -> YES
        ("m2", "resolved_or_removed", None, "2026-06-02", True),   # candidate -> ambiguous
        ("m3", "resolved_or_removed", None, "2026-06-03", True),   # candidate -> NO
        ("m4", "resolved_or_removed", None, "2026-06-04", False),  # no snapshot -> excluded
        ("m5", "active", None, "2026-06-05", True),                # active -> excluded
        ("m6", "resolved_or_removed", "1", "2026-06-06", True),    # already backfilled
    ]
    for mid, status, res, lu, has_snap in rows:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?)", (mid, status, res, lu)
        )
        if has_snap:
            conn.execute("INSERT INTO snapshots(market_id) VALUES (?)", (mid,))
    conn.commit()
    return conn


def test_fetch_missing_resolutions_updates_definitive_only(tmp_path):
    conn = _make_db(tmp_path)
    outcomes = {"m1": 1, "m2": None, "m3": 0}

    def fake_fetcher(session, market_id):
        return outcomes[market_id]

    updated = fetch_missing_resolutions(conn, session=object(), fetcher=fake_fetcher)
    assert updated == 2  # m1 and m3; m2 ambiguous stays NULL

    got = dict(conn.execute("SELECT id, resolution FROM markets").fetchall())
    # The resolution column is TEXT (real schema), so SQLite's type affinity
    # stores the bound ints as '1'/'0' strings — exactly the form that
    # features.labels._normalize_resolution already accepts.
    assert got["m1"] == "1"
    assert got["m2"] is None
    assert got["m3"] == "0"
    assert got["m4"] is None   # untouched — no snapshot
    assert got["m5"] is None   # untouched — active
    assert got["m6"] == "1"    # untouched — already set
    conn.close()


def test_fetch_missing_resolutions_respects_limit(tmp_path):
    conn = _make_db(tmp_path)
    seen: list[str] = []

    def fake_fetcher(session, market_id):
        seen.append(market_id)
        return 1

    updated = fetch_missing_resolutions(conn, session=object(), fetcher=fake_fetcher, limit=1)
    assert updated == 1
    assert len(seen) == 1
    # Newest last_updated first -> m3 (2026-06-03) is picked.
    assert seen == ["m3"]
    conn.close()


def test_fetch_missing_resolutions_survives_fetcher_errors(tmp_path):
    conn = _make_db(tmp_path)

    def exploding_fetcher(session, market_id):
        if market_id == "m3":
            raise RuntimeError("boom")
        return 0

    updated = fetch_missing_resolutions(conn, session=object(), fetcher=exploding_fetcher)
    # m3 exploded (skipped), m1 + m2 resolved to 0.
    assert updated == 2
    got = dict(conn.execute("SELECT id, resolution FROM markets").fetchall())
    assert got["m3"] is None
    conn.close()


def test_fetch_missing_resolutions_empty_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.execute("CREATE TABLE markets (id TEXT, status TEXT, resolution TEXT, last_updated TEXT)")
    conn.execute("CREATE TABLE snapshots (id INTEGER, market_id TEXT)")
    conn.commit()
    assert fetch_missing_resolutions(conn, session=object(), fetcher=lambda s, m: 1) == 0
    conn.close()
