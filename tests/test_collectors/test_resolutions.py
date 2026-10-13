"""Tests for the resolution-capture helpers in collectors.collect_markets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from collectors.collect_markets import (
    _parse_outcome_prices,
    fetch_market_resolution,
    fetch_market_status,
    fetch_missing_resolutions,
    probe_scored_unresolved,
)


# _parse_outcome_prices


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


# fetch_market_resolution (canned-response session)


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


# fetch_missing_resolutions (synthetic DB + stubbed fetcher)


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


# fetch_market_status (canned-response session)


def test_fetch_status_closed_definitive():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": True, "outcomePrices": '["1", "0"]',
    }))
    assert fetch_market_status(session, "x") == ("closed", 1)


def test_fetch_status_closed_ambiguous():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": True, "outcomePrices": '["0.56", "0.44"]',
    }))
    assert fetch_market_status(session, "x") == ("closed", None)


def test_fetch_status_open():
    session = _FakeSession(_FakeResponse(200, {
        "id": "x", "closed": False, "outcomePrices": '["1", "0"]',
    }))
    assert fetch_market_status(session, "x") == ("open", None)


def test_fetch_status_gone():
    session = _FakeSession(_FakeResponse(404))
    assert fetch_market_status(session, "x") == ("gone", None)
    assert session.calls == 1  # no retries on 404


# probe_scored_unresolved (synthetic DB + stubbed fetcher)


def _make_probe_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "probe.db")
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, status TEXT, "
        "resolution TEXT, last_updated TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, midpoint REAL)"
    )
    conn.execute(
        "CREATE TABLE llm_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, retrospective INTEGER)"
    )

    def add(mid, status="active", resolution=None, scored=True, retrospective=0,
            last_mid=0.50):
        conn.execute("INSERT INTO markets VALUES (?, ?, ?, '2026-07-01')",
                     (mid, status, resolution))
        if last_mid is not None:
            # two snapshots so "latest" ordering matters
            conn.execute(
                "INSERT INTO snapshots (market_id, timestamp, midpoint) "
                "VALUES (?, '2026-07-01T00:00:00', 0.50)", (mid,))
            conn.execute(
                "INSERT INTO snapshots (market_id, timestamp, midpoint) "
                "VALUES (?, '2026-07-02T00:00:00', ?)", (mid, last_mid))
        if scored:
            conn.execute(
                "INSERT INTO llm_scores (market_id, retrospective) VALUES (?, ?)",
                (mid, retrospective))

    add("m_yes", last_mid=0.99)       # pinned high -> closed YES
    add("m_no", last_mid=0.01)        # pinned low  -> closed NO
    add("m_ambig", last_mid=0.99)     # pinned -> closed but ambiguous outcome
    add("m_open", last_mid=0.50)      # unpinned -> still open on Gamma
    add("m_gone", last_mid=0.50)      # 404 on Gamma
    add("m_unscored", scored=False)             # no llm score -> not a candidate
    add("m_retro", retrospective=1)             # retrospective-only -> not a candidate
    add("m_resolved", status="resolved_or_removed")  # already flipped -> not a candidate
    conn.commit()
    return conn


GAMMA_STATES = {
    "m_yes": ("closed", 1),
    "m_no": ("closed", 0),
    "m_ambig": ("closed", None),
    "m_open": ("open", None),
    "m_gone": ("gone", None),
}


def test_probe_flips_closed_and_gone_only(tmp_path):
    conn = _make_probe_db(tmp_path)
    probed: list[str] = []

    def fake_fetcher(session, market_id):
        probed.append(market_id)
        return GAMMA_STATES[market_id]

    flipped, resolved = probe_scored_unresolved(
        conn, session=object(), fetcher=fake_fetcher, now_iso="2026-07-23T00:00:00+00:00",
    )
    assert flipped == 4      # m_yes, m_no, m_ambig, m_gone
    assert resolved == 2     # m_yes, m_no
    # only candidates were probed — never the unscored/retro/already-resolved
    assert set(probed) == {"m_yes", "m_no", "m_ambig", "m_open", "m_gone"}

    got = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, status, resolution, last_updated FROM markets").fetchall()}
    assert got["m_yes"] == ("resolved_or_removed", "1", "2026-07-23T00:00:00+00:00")
    assert got["m_no"] == ("resolved_or_removed", "0", "2026-07-23T00:00:00+00:00")
    assert got["m_ambig"] == ("resolved_or_removed", None, "2026-07-23T00:00:00+00:00")
    assert got["m_gone"] == ("resolved_or_removed", None, "2026-07-23T00:00:00+00:00")
    # open market untouched
    assert got["m_open"] == ("active", None, "2026-07-01")
    assert got["m_unscored"][0] == "active"
    assert got["m_retro"][0] == "active"
    conn.close()


def test_probe_pinned_markets_probed_first(tmp_path):
    conn = _make_probe_db(tmp_path)
    probed: list[str] = []

    def fake_fetcher(session, market_id):
        probed.append(market_id)
        return GAMMA_STATES[market_id]

    # limit=3: exactly the three pinned candidates, unpinned never probed
    probe_scored_unresolved(conn, session=object(), fetcher=fake_fetcher, limit=3)
    assert set(probed) == {"m_yes", "m_no", "m_ambig"}   # the pinned ones
    conn.close()


def test_probe_survives_fetcher_errors(tmp_path):
    conn = _make_probe_db(tmp_path)

    def exploding_fetcher(session, market_id):
        if market_id == "m_yes":
            raise RuntimeError("boom")
        return GAMMA_STATES[market_id]

    flipped, resolved = probe_scored_unresolved(
        conn, session=object(), fetcher=exploding_fetcher)
    assert flipped == 3      # m_no, m_ambig, m_gone (m_yes exploded, skipped)
    assert resolved == 1     # m_no
    row = conn.execute("SELECT status FROM markets WHERE id='m_yes'").fetchone()
    assert row[0] == "active"   # untouched, retried next run
    conn.close()


def test_probe_missing_llm_scores_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    conn.execute("CREATE TABLE markets (id TEXT, status TEXT, resolution TEXT, "
                 "last_updated TEXT)")
    conn.execute("CREATE TABLE snapshots (id INTEGER, market_id TEXT, "
                 "timestamp TEXT, midpoint REAL)")
    conn.commit()
    assert probe_scored_unresolved(conn, session=object()) == (0, 0)
    conn.close()


def test_probe_then_resolution_pass_retries_ambiguous(tmp_path):
    conn = _make_probe_db(tmp_path)
    probe_scored_unresolved(
        conn, session=object(),
        fetcher=lambda s, m: GAMMA_STATES[m],
        now_iso="2026-07-23T00:00:00+00:00",
    )
    # Next collector run: Gamma has settled m_ambig to YES.
    updated = fetch_missing_resolutions(
        conn, session=object(),
        fetcher=lambda s, m: 1 if m == "m_ambig" else None,
    )
    assert updated == 1
    row = conn.execute("SELECT resolution FROM markets WHERE id='m_ambig'").fetchone()
    assert row[0] == "1"
    conn.close()
