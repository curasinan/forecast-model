"""Tests for monitoring.backup_db — consistent copy + rotation pruning."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from monitoring.backup_db import _prune_old_backups, backup_database


def _make_source_db(tmp_path: Path, n_rows: int = 5) -> Path:
    db = tmp_path / "source.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, market_id TEXT, midpoint REAL)"
    )
    conn.executemany(
        "INSERT INTO snapshots(market_id, midpoint) VALUES (?, ?)",
        [(f"m{i}", 0.1 * i) for i in range(n_rows)],
    )
    conn.commit()
    conn.close()
    return db


def test_backup_creates_valid_openable_copy(tmp_path):
    src = _make_source_db(tmp_path, n_rows=5)
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 6, 29, 3, 0, 0)

    dest = backup_database(db_path=src, backup_dir=backup_dir, keep=7, now=now)

    assert dest.exists()
    assert dest.name == "polymarket_20260629_030000.db"
    # The copy must be a valid, queryable SQLite DB with all rows.
    conn = sqlite3.connect(dest)
    try:
        count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    assert count == 5


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(
            db_path=tmp_path / "does_not_exist.db",
            backup_dir=tmp_path / "backups",
        )


def test_prune_keeps_exactly_seven_newest(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    names = [f"polymarket_2026060{i}_030000.db" for i in range(1, 10)]  # 9 files
    names.append("polymarket_20260610_030000.db")                       # 10th (newest)
    for name in names:
        (backup_dir / name).write_bytes(b"stub")

    deleted = _prune_old_backups(backup_dir, keep=7)

    remaining = sorted(p.name for p in backup_dir.glob("polymarket_*.db"))
    assert len(remaining) == 7
    assert len(deleted) == 3
    # The three OLDEST (lexicographically smallest stamps) are the ones gone.
    assert sorted(p.name for p in deleted) == names[:3]
    # Newest is retained.
    assert "polymarket_20260610_030000.db" in remaining


def test_prune_noop_under_threshold(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(1, 4):  # only 3 files
        (backup_dir / f"polymarket_2026060{i}_030000.db").write_bytes(b"stub")

    deleted = _prune_old_backups(backup_dir, keep=7)

    assert deleted == []
    assert len(list(backup_dir.glob("polymarket_*.db"))) == 3


def test_end_to_end_rotation_via_backup_database(tmp_path):
    """Running backup_database repeatedly never exceeds the retention cap."""
    src = _make_source_db(tmp_path)
    backup_dir = tmp_path / "backups"
    for day in range(1, 10):  # 9 runs, keep=7
        backup_database(
            db_path=src, backup_dir=backup_dir, keep=7,
            now=datetime(2026, 6, day, 3, 0, 0),
        )
    remaining = sorted(p.name for p in backup_dir.glob("polymarket_*.db"))
    assert len(remaining) == 7
    # Oldest two runs (day 1, 2) pruned; newest (day 9) present.
    assert remaining[0] == "polymarket_20260603_030000.db"
    assert remaining[-1] == "polymarket_20260609_030000.db"
