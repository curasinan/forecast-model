"""scripts/spark/restore_check.py: restore a copy into a scratch root and verify it."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import restore_check  # noqa: E402

from monitoring.backup_paper_book import MANIFEST_NAME, run_backup  # noqa: E402

BOOK = ("ibkr_ticks", "paper", "ibkr_paper.db")
MODEL = ("climate_data", "models", "gtm_emos.json")
LEDGER = ("climate_data", "labels", "labels_ledger.parquet")
RUNS = ("ibkr_ticks", "paper", "ibkr_sync_runs.jsonl")


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "quant_data"
    book = root.joinpath(*BOOK)
    book.parent.mkdir(parents=True)
    # closing(): a connection that is only left behind keeps the file locked on Windows
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE positions (question_id TEXT, size REAL)")
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO trade_log VALUES ('a', 'open')")
        conn.commit()
    root.joinpath(*RUNS).write_text('{"ok": true}\n{"ok": false}\n', encoding="utf-8")
    model = root.joinpath(*MODEL)
    model.parent.mkdir(parents=True)
    model.write_text('{"trend": true}', encoding="utf-8")
    ledger = root.joinpath(*LEDGER)
    ledger.parent.mkdir(parents=True)
    pd.DataFrame({"series": ["gtm"], "value": [1.18]}).to_parquet(ledger, index=False)
    return root


@pytest.fixture
def day_dir(root, tmp_path) -> Path:
    return run_backup(root, tmp_path / "backups", date(2026, 10, 20))


def test_a_good_day_directory_restores_and_verifies(day_dir, tmp_path, capsys):
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK" in out
    assert (tmp_path / "scratch").joinpath(*BOOK).is_file()


def test_a_whole_root_without_a_manifest_can_be_checked_too(root, tmp_path, capsys):
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    assert rc == 0, capsys.readouterr().out


def test_a_file_changed_after_the_manifest_was_written_fails(day_dir, tmp_path, capsys):
    day_dir.joinpath(*MODEL).write_text('{"trend": false}', encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL climate_data/models/gtm_emos.json" in out and "sha256" in out


def test_a_file_the_manifest_lists_but_the_copy_lacks_fails(day_dir, tmp_path, capsys):
    day_dir.joinpath(*RUNS).unlink()
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl: missing" in out


def test_a_truncated_paper_book_fails_even_when_the_manifest_was_written_from_it(
        root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    book.write_bytes(book.read_bytes()[:100])  # the header only: not a database any more
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db" in out


def test_a_paper_book_without_its_tables_fails(root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    book.unlink()
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "trade_log" in out


def test_an_unreadable_parquet_and_a_broken_ledger_line_fail(root, tmp_path, capsys):
    root.joinpath(*LEDGER).write_bytes(b"not parquet")
    root.joinpath(*RUNS).write_text('{"ok": true}\nnot json\n', encoding="utf-8")
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL climate_data/labels/labels_ledger.parquet" in out
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl: line 2" in out


def test_a_scratch_directory_that_holds_something_is_refused(day_dir, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "keep.txt").write_text("mine", encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    assert rc == 2
    assert (scratch / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))  # source untouched


def test_happy_path_asserts_file_count_and_book_exists(day_dir, tmp_path, capsys):
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK: 4 files" in out
    assert (tmp_path / "scratch").joinpath(*BOOK).is_file()


def test_whole_root_without_paper_book_fails(root, tmp_path, capsys):
    root.joinpath(*BOOK).unlink()
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: the copy has no paper book" in out


def test_day_dir_with_paper_book_null_in_manifest_fails(day_dir, tmp_path, capsys):
    day_dir.joinpath(*BOOK).unlink()
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["ibkr_ticks/paper/ibkr_paper.db"] = None
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: the copy has no paper book" in out


def test_empty_directory_fails_no_book(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = restore_check.main(["--from", str(empty), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: the copy has no paper book" in out


def test_nonexistent_copy_path_returns_2(tmp_path, capsys):
    rc = restore_check.main(["--from", str(tmp_path / "nonexistent"), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "cannot read the copy" in out and "is not a directory" in out


def test_manifest_with_truncated_json_returns_2(day_dir, tmp_path, capsys):
    (day_dir / MANIFEST_NAME).write_text('{"incomplete":', encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "cannot read the copy" in out
    assert "Traceback" not in out


def test_scratch_that_is_a_file_returns_2(day_dir, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    scratch.write_text("i am a file", encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 2
    # Scratch that is a file is treated as "not empty"
    assert "refusing" in out


def test_jsonl_with_invalid_utf8_fails_but_checks_other_files(root, tmp_path, capsys):
    root.joinpath(*RUNS).write_bytes(b'{"ok": true}\n\xff\xfe\n')
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl" in out
    # Other files should still be checked and found good
    assert "FAIL climate_data/models/gtm_emos.json" not in out


def test_paper_book_with_failed_integrity_check_fails(root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    # Read the full book, zero middle bytes to corrupt it
    data = bytearray(book.read_bytes())
    if len(data) > 200:
        # Zero bytes in the middle to corrupt the database structure
        data[100:300] = b'\x00' * 200
    book.write_bytes(bytes(data))
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    # The corrupted database either fails on integrity_check or fails to open at all
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db" in out and ("integrity check" in out or "database" in out)


def test_manifest_key_with_path_escape_fails(day_dir, tmp_path, capsys):
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["../escape.txt"] = {"sha256": "abc123", "size": 11}
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    # Create the file so it can be copied
    (day_dir / ".." / "escape.txt").parent.mkdir(exist_ok=True)
    (day_dir / ".." / "escape.txt").write_text("escape file", encoding="utf-8")
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ../escape.txt: path escapes the scratch root" in out
    # Verify nothing was written outside scratch
    assert not (scratch.parent / "escape.txt").exists()
