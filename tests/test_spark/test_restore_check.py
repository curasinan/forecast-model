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
