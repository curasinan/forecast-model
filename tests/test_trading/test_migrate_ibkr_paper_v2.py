"""Task 1.4: migrate_ibkr_paper_v2 adds the v2 schema (fill_events table +
trade_log.perm_id/last_synced_utc) to a v1 `ibkr_paper.db` and backfills
permId for the three legacy `placed` rows that predate Task 1.3's
perm_id column, from an operator-supplied JSON map.

The v1 schema below is copied literally from
src/trading/position_tracker.py (the CREATE TABLE statements as they were
before Task 1.3 added perm_id/last_synced_utc to trade_log) so the test
exercises the real pre-migration shape rather than PositionTracker's
current (already-migrated) init path.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import sys

import pytest

from scripts.migrations.migrate_ibkr_paper_v2 import migrate

V1_POSITIONS_DDL = """
    CREATE TABLE IF NOT EXISTS positions (
        question_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        entry_price REAL NOT NULL,
        size REAL NOT NULL,
        cost_basis REAL NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        exit_price REAL,
        realized_pnl REAL,
        PRIMARY KEY (question_id, outcome, opened_at)
    )
"""

V1_TRADE_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS trade_log (
        order_id TEXT PRIMARY KEY,
        question_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        side TEXT NOT NULL,
        price REAL NOT NULL,
        size REAL NOT NULL,
        cost_usd REAL NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        filled_at TEXT
    )
"""

POSITIONS_V1_COLUMNS = {
    "question_id", "outcome", "entry_price", "size", "cost_basis",
    "opened_at", "closed_at", "exit_price", "realized_pnl",
}

# The three legacy rows this migration exists to fix (real sizes/prices
# from 2026-08-25, captured before perm_id existed).
LEGACY_ROWS = [
    ("a1", "q1", "YES", "BUY", 0.34, 29, 9.86, "placed", None, "2026-08-25T10:00:00", None),
    ("a2", "q2", "YES", "BUY", 0.24, 41, 9.84, "placed", None, "2026-08-25T10:05:00", None),
    ("a3", "q1", "NO", "BUY", 0.14, 71, 9.94, "placed", None, "2026-08-25T10:10:00", None),
]


def _make_v1_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(V1_POSITIONS_DDL)
    conn.execute(V1_TRADE_LOG_DDL)
    conn.executemany(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd,
            status, reason, created_at, filled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        LEGACY_ROWS,
    )
    conn.commit()
    conn.close()


def _columns(conn, table) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migrate_adds_columns_and_fill_events_table(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    result = migrate(str(db))

    assert set(result["columns_added"]) == {"perm_id", "last_synced_utc"}
    assert result["fill_events_created"] is True
    assert result["permids_backfilled"] == 0

    conn = sqlite3.connect(db)
    assert {"perm_id", "last_synced_utc"} <= _columns(conn, "trade_log")
    assert "fill_events" in _tables(conn)
    fill_events_cols = _columns(conn, "fill_events")
    conn.close()
    assert fill_events_cols == {
        "perm_id", "order_id", "ts_utc", "detected_ts_utc", "cum_filled",
        "fill_delta", "avg_price", "commission_usd", "source",
    }


def test_migrate_leaves_positions_untouched(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    migrate(str(db))

    conn = sqlite3.connect(db)
    cols = _columns(conn, "positions")
    conn.close()
    assert cols == POSITIONS_V1_COLUMNS


def test_migrate_schema_only_when_no_permid_map(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    result = migrate(str(db))

    assert result["permids_backfilled"] == 0
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT order_id, perm_id FROM trade_log").fetchall())
    conn.close()
    assert rows == {"a1": None, "a2": None, "a3": None}


def test_migrate_backfills_dict_shaped_permid_map(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    result = migrate(str(db), {"a1": "111", "a2": "222", "a3": "333"})

    assert result["permids_backfilled"] == 3
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT order_id, perm_id FROM trade_log").fetchall())
    conn.close()
    assert rows == {"a1": "111", "a2": "222", "a3": "333"}


def test_migrate_dict_map_unknown_order_id_raises(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    with pytest.raises(ValueError, match="zzz"):
        migrate(str(db), {"a1": "111", "zzz": "999"})

    # Validation failed before any write -- fully rolled back, including schema DDL:
    # perm_id was never added and fill_events was never created.
    conn = sqlite3.connect(db)
    order_ids = {row[0] for row in conn.execute("SELECT order_id FROM trade_log")}
    tables = _tables(conn)
    cols = _columns(conn, "trade_log")
    conn.close()
    assert order_ids == {"a1", "a2", "a3"}
    assert "fill_events" not in tables
    assert "perm_id" not in cols


def test_migrate_backfills_list_shaped_permid_map(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    # Real shape from the operator's reqAllOpenOrders capture console.
    operator_capture = [
        {"order_id_venue": "V1", "perm_id": 111, "con_id": 1, "local_symbol": "X",
         "action": "BUY", "qty": 29, "lmt": 0.34, "status": "PreSubmitted"},
        {"order_id_venue": "V2", "perm_id": 222, "con_id": 2, "local_symbol": "Y",
         "action": "BUY", "qty": 41, "lmt": 0.24, "status": "PreSubmitted"},
        {"order_id_venue": "V3", "perm_id": 333, "con_id": 1, "local_symbol": "Z",
         "action": "BUY", "qty": 71, "lmt": 0.14, "status": "PreSubmitted"},
        # Decoy: doesn't match any trade_log row (qty/lmt don't line up) -- must not error.
        {"order_id_venue": "V4", "perm_id": 444, "con_id": 3, "local_symbol": "W",
         "action": "SELL", "qty": 5, "lmt": 0.99, "status": "PreSubmitted"},
    ]

    result = migrate(str(db), operator_capture)

    assert result["permids_backfilled"] == 3
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT order_id, perm_id FROM trade_log").fetchall())
    conn.close()
    assert rows == {"a1": "111", "a2": "222", "a3": "333"}


def test_migrate_list_map_ambiguous_db_match_raises(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)
    # A second row with the same size+price as a1 makes qty+lmt no longer unique.
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO trade_log
           (order_id, question_id, outcome, side, price, size, cost_usd,
            status, reason, created_at, filled_at)
           VALUES ('a4', 'q3', 'YES', 'BUY', 0.34, 29, 9.86, 'placed', NULL,
                   '2026-08-25T10:15:00', NULL)"""
    )
    conn.commit()
    conn.close()

    operator_capture = [
        {"order_id_venue": "V1", "perm_id": 111, "con_id": 1, "local_symbol": "X",
         "action": "BUY", "qty": 29, "lmt": 0.34, "status": "PreSubmitted"},
    ]

    with pytest.raises(ValueError, match="(?i)ambiguous"):
        migrate(str(db), operator_capture)


def test_migrate_list_map_ambiguous_conflicting_entries_raises(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    # Two capture entries both resolve (uniquely) to a1, but disagree on the permId.
    operator_capture = [
        {"order_id_venue": "V1", "perm_id": 111, "con_id": 1, "local_symbol": "X",
         "action": "BUY", "qty": 29, "lmt": 0.34, "status": "PreSubmitted"},
        {"order_id_venue": "V1dup", "perm_id": 999, "con_id": 1, "local_symbol": "X",
         "action": "BUY", "qty": 29, "lmt": 0.34, "status": "PreSubmitted"},
    ]

    with pytest.raises(ValueError, match="(?i)ambiguous"):
        migrate(str(db), operator_capture)


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    first = migrate(str(db), {"a1": "111", "a2": "222", "a3": "333"})
    assert first["permids_backfilled"] == 3
    assert first["fill_events_created"] is True
    assert set(first["columns_added"]) == {"perm_id", "last_synced_utc"}

    conn = sqlite3.connect(db)
    before = conn.execute(
        "SELECT order_id, perm_id, status FROM trade_log ORDER BY order_id"
    ).fetchall()
    conn.close()

    second = migrate(str(db), {"a1": "111", "a2": "222", "a3": "333"})

    conn = sqlite3.connect(db)
    after = conn.execute(
        "SELECT order_id, perm_id, status FROM trade_log ORDER BY order_id"
    ).fetchall()
    conn.close()

    assert second["columns_added"] == []
    assert second["fill_events_created"] is False
    assert second["permids_backfilled"] == 0
    assert before == after


def test_migrate_idempotent_with_task_1_3_ensure_columns(tmp_path):
    """Task 1.3's PositionTracker._ensure_columns may have already added
    perm_id/last_synced_utc (and other venue-neutral columns) to trade_log
    before this migration ever runs. Adding an already-present column must
    stay a no-op.
    """
    from src.trading.position_tracker import PositionTracker

    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    conn = sqlite3.connect(db)
    PositionTracker._ensure_columns(
        conn, "trade_log",
        {"market_id": "TEXT", "venue": "TEXT", "venue_order_id": "TEXT",
         "filled_size": "REAL", "perm_id": "TEXT", "last_synced_utc": "TEXT"},
    )
    conn.commit()
    conn.close()

    result = migrate(str(db), {"a1": "111", "a2": "222", "a3": "333"})

    assert result["columns_added"] == []  # perm_id/last_synced_utc already existed
    assert result["permids_backfilled"] == 3


def test_cli_schema_only(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)

    proc = subprocess.run(
        [sys.executable, "scripts/migrations/migrate_ibkr_paper_v2.py", "--db", str(db)],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["fill_events_created"] is True
    assert output["permids_backfilled"] == 0


def test_cli_with_permid_map_file(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)
    permid_map_path = tmp_path / "permid_map.json"
    permid_map_path.write_text(json.dumps({"a1": "111", "a2": "222", "a3": "333"}))

    proc = subprocess.run(
        [
            sys.executable, "scripts/migrations/migrate_ibkr_paper_v2.py",
            "--db", str(db), "--permid-map", str(permid_map_path),
        ],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["permids_backfilled"] == 3


def test_cli_reports_error_and_nonzero_exit_on_unknown_order_id(tmp_path):
    db = tmp_path / "ibkr_paper.db"
    _make_v1_db(db)
    permid_map_path = tmp_path / "permid_map.json"
    permid_map_path.write_text(json.dumps({"zzz": "999"}))

    proc = subprocess.run(
        [
            sys.executable, "scripts/migrations/migrate_ibkr_paper_v2.py",
            "--db", str(db), "--permid-map", str(permid_map_path),
        ],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode != 0
    assert "zzz" in proc.stderr
