"""monitoring/backup_paper_book.py: a daily, consistent, rotated copy of the operational files."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

from analysis.spark_parity import MANIFEST_FILES
from monitoring import backup_paper_book
from monitoring.backup_paper_book import KEEP_DAYS, MANIFEST_NAME, PAPER_BOOK, main, run_backup

BOOK = ("ibkr_ticks", "paper", "ibkr_paper.db")
MODEL = ("climate_data", "models", "gtm_emos.json")
TODAY = date(2026, 10, 20)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def root(tmp_path) -> Path:
    """A quant-data root holding a paper book with three orders and a model file."""
    root = tmp_path / "quant_data"
    book = root.joinpath(*BOOK)
    book.parent.mkdir(parents=True)
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.executemany("INSERT INTO trade_log VALUES (?, ?)",
                         [("a", "open"), ("b", "open"), ("c", "open")])
        conn.commit()
    model = root.joinpath(*MODEL)
    model.parent.mkdir(parents=True)
    model.write_text('{"trend": true}', encoding="utf-8")
    return root


def test_both_files_under_test_are_operational_files():
    assert BOOK in MANIFEST_FILES and MODEL in MANIFEST_FILES


def test_the_copy_and_its_manifest_agree(root, tmp_path):
    day_dir = run_backup(root, tmp_path / "backups", TODAY)

    assert day_dir == tmp_path / "backups" / "20261020"
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest) == {"/".join(parts) for parts in MANIFEST_FILES}
    model_copy = day_dir.joinpath(*MODEL)
    assert model_copy.read_text(encoding="utf-8") == '{"trend": true}'
    assert manifest["/".join(MODEL)] == {"sha256": _sha256(model_copy),
                                         "size": model_copy.stat().st_size}
    assert manifest["/".join(BOOK)]["sha256"] == _sha256(day_dir.joinpath(*BOOK))
    assert manifest["climate_data/labels/labels_ledger.parquet"] is None


def test_the_paper_book_copy_is_consistent_while_a_writer_is_mid_transaction(root, tmp_path):
    writer = sqlite3.connect(root.joinpath(*BOOK), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO trade_log VALUES ('d', 'open')")  # not committed

        day_dir = run_backup(root, tmp_path / "backups", TODAY)
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    with closing(sqlite3.connect(day_dir.joinpath(*BOOK))) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert [row[0] for row in copy.execute("SELECT order_id FROM trade_log ORDER BY 1")] \
            == ["a", "b", "c"]


def test_only_the_newest_days_are_kept_and_other_directories_are_left_alone(root, tmp_path):
    dest = tmp_path / "backups"
    for age in range(1, KEEP_DAYS + 3):  # 32 older days
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)
    (dest / "notes").mkdir()

    run_backup(root, dest, TODAY)

    days = sorted(p.name for p in dest.iterdir() if p.name.isdigit())
    assert len(days) == KEEP_DAYS
    assert days[-1] == "20261020"
    assert days[0] == (TODAY - timedelta(days=KEEP_DAYS - 1)).strftime("%Y%m%d")
    assert (dest / "notes").is_dir()


def test_a_second_run_on_the_same_day_replaces_the_copy_and_leaves_no_partial(root, tmp_path):
    dest = tmp_path / "backups"
    run_backup(root, dest, TODAY)
    root.joinpath(*MODEL).write_text('{"trend": false}', encoding="utf-8")

    day_dir = run_backup(root, dest, TODAY)

    assert day_dir.joinpath(*MODEL).read_text(encoding="utf-8") == '{"trend": false}'
    assert sorted(p.name for p in dest.iterdir()) == ["20261020"]


def test_main_exits_0_and_names_the_day_it_was_given(root, tmp_path, capsys):
    rc = main(["--root", str(root), "--dest", str(tmp_path / "backups")], today=TODAY)
    assert rc == 0
    assert (tmp_path / "backups" / "20261020" / MANIFEST_NAME).is_file()
    assert "20261020" in capsys.readouterr().out


def test_main_exits_1_when_the_copy_cannot_be_made(root, tmp_path, capsys):
    blocker = tmp_path / "backups"
    blocker.write_text("a file where the backup directory should be", encoding="utf-8")

    rc = main(["--root", str(root), "--dest", str(blocker)], today=TODAY)

    assert rc == 1
    assert "backup failed" in capsys.readouterr().out


# ---- Additional tests for plan-mandated fixes ----
SIGNALS_DIR = ("climate_data", "paper", "gtm_signals.json")


def test_wal_rows_committed_but_not_checkpointed_are_in_the_copy(root, tmp_path):
    """WAL-mode rows committed but not checkpointed must be in the backup (uses online backup API)."""
    book = root.joinpath(*BOOK)
    writer = sqlite3.connect(book, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO trade_log VALUES ('d', 'open')")
        day_dir = run_backup(root, tmp_path / "backups", TODAY)
    finally:
        writer.close()
    with closing(sqlite3.connect(day_dir.joinpath(*BOOK))) as copy:
        assert [r[0] for r in copy.execute("SELECT order_id FROM trade_log ORDER BY 1")] == ["a", "b", "c", "d"]


def test_a_failed_copy_leaves_no_day_directory_and_no_manifest(root, tmp_path):
    """A failed copy must not leave a day directory or a manifest anywhere under dest."""
    dest = tmp_path / "backups"
    root.joinpath(*SIGNALS_DIR).mkdir(parents=True)
    with pytest.raises(OSError):
        run_backup(root, dest, TODAY)
    assert not (dest / "20261020").exists()
    assert not list(dest.rglob(MANIFEST_NAME))


def test_a_leftover_partial_from_a_crash_is_not_part_of_the_next_copy(root, tmp_path):
    """A pre-existing .partial from a crashed run must not appear in the next copy."""
    dest = tmp_path / "backups"
    stale = dest / "20261020.partial" / "junk.bin"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    day_dir = run_backup(root, dest, TODAY)
    assert not (day_dir / "junk.bin").exists()
    assert sorted(p.name for p in dest.iterdir()) == ["20261020"]


def test_the_day_just_written_survives_the_rotation_even_with_a_clock_behind(root, tmp_path):
    """The day directory just written must never be pruned, even if today is older than kept days."""
    dest = tmp_path / "backups"
    for age in range(1, 4):
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)
    day_dir = run_backup(root, dest, TODAY - timedelta(days=10), keep=3)
    assert day_dir.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_umask_is_applied_to_file_copies(root, tmp_path):
    """File copies must get 0666 & ~umask permissions, not the source's permissions."""
    old_umask = os.umask(0o027)
    try:
        src = root.joinpath(*MODEL)
        src.chmod(0o600)
        day_dir = run_backup(root, tmp_path / "backups", TODAY)
        copy = day_dir.joinpath(*MODEL)
        assert oct(copy.stat().st_mode)[-3:] == "640"
    finally:
        os.umask(old_umask)


def test_main_refuses_a_root_that_is_not_a_directory(tmp_path, capsys):
    """main must refuse and exit 1 when root is not a directory, without creating anything under dest."""
    root = tmp_path / "not_a_dir"
    root.write_text("a file", encoding="utf-8")
    dest = tmp_path / "backups"

    rc = main(["--root", str(root), "--dest", str(dest)], today=TODAY)

    assert rc == 1
    assert "is not a directory" in capsys.readouterr().out
    assert not dest.exists()


def test_main_refuses_a_root_without_the_paper_book(tmp_path, capsys):
    """main must refuse and exit 1 when the paper book is missing, without creating anything under dest."""
    root = tmp_path / "quant_data"
    root.mkdir()
    dest = tmp_path / "backups"

    rc = main(["--root", str(root), "--dest", str(dest)], today=TODAY)

    assert rc == 1
    assert "is missing" in capsys.readouterr().out
    assert not dest.exists()


def test_the_paper_book_is_named_by_its_file_not_by_its_place_in_the_manifest():
    assert "/".join(PAPER_BOOK) == "ibkr_ticks/paper/ibkr_paper.db"
    assert PAPER_BOOK == BOOK
    assert PAPER_BOOK in MANIFEST_FILES  # so the copy loop takes it


SYNC_LEDGER = ("ibkr_ticks", "paper", "ibkr_sync_runs.jsonl")


def _book_last() -> tuple:
    """MANIFEST_FILES in another order: the sync ledger first, the paper book last."""
    rest = [parts for parts in MANIFEST_FILES if parts not in (BOOK, SYNC_LEDGER)]
    return (SYNC_LEDGER, *rest, BOOK)


def test_a_reordered_manifest_still_refuses_a_root_without_the_paper_book(
        tmp_path, monkeypatch, capsys):
    reordered = _book_last()
    monkeypatch.setattr(backup_paper_book, "MANIFEST_FILES", reordered)
    root = tmp_path / "quant_data"
    first = root.joinpath(*reordered[0])
    first.parent.mkdir(parents=True)
    first.write_text('{"ok": true}\n', encoding="utf-8")
    dest = tmp_path / "backups"

    rc = main(["--root", str(root), "--dest", str(dest)], today=TODAY)

    assert rc == 1
    out = capsys.readouterr().out
    assert "ibkr_paper.db" in out and "is missing" in out
    assert not dest.exists()


def test_a_reordered_manifest_does_not_refuse_a_root_that_holds_the_paper_book(
        root, tmp_path, monkeypatch):
    reordered = _book_last()
    assert not root.joinpath(*reordered[0]).exists()
    monkeypatch.setattr(backup_paper_book, "MANIFEST_FILES", reordered)

    day_dir = run_backup(root, tmp_path / "backups", TODAY)

    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["/".join(BOOK)]["sha256"] == _sha256(day_dir.joinpath(*BOOK))


def test_rotation_with_keep_1_leaves_only_the_new_day(root, tmp_path):
    """With keep=1, rotation must keep only the newest day (today)."""
    dest = tmp_path / "backups"
    for age in range(1, 11):  # 10 older days
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)

    run_backup(root, dest, TODAY, keep=1)

    days = sorted(p.name for p in dest.iterdir() if p.name.isdigit())
    assert days == ["20261020"]


def test_rotation_with_keep_0_removes_nothing(root, tmp_path):
    """With keep=0 (nonsense value), rotation must remove nothing to avoid deleting history."""
    dest = tmp_path / "backups"
    for age in range(1, 11):  # 10 older days
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)

    run_backup(root, dest, TODAY, keep=0)

    days = sorted(p.name for p in dest.iterdir() if p.name.isdigit())
    assert len(days) == 11  # 10 old + today


def test_rotation_with_keep_30_and_40_older_days_leaves_exactly_30(root, tmp_path):
    """With keep=30, 40 older days, rotation must leave exactly 30 days with the oldest being today minus 29 days."""
    dest = tmp_path / "backups"
    for age in range(1, 41):  # 40 older days
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)

    run_backup(root, dest, TODAY, keep=30)

    days = sorted(p.name for p in dest.iterdir() if p.name.isdigit())
    assert len(days) == 30
    assert days[-1] == "20261020"  # today
    assert days[0] == (TODAY - timedelta(days=29)).strftime("%Y%m%d")  # today minus 29
