"""scripts/spark/restore_check.py: restore a copy into a scratch root and verify it."""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import restore_check  # noqa: E402

from analysis import spark_parity  # noqa: E402
from monitoring.backup_paper_book import MANIFEST_NAME, run_backup, file_record  # noqa: E402

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
    assert "RESTORE OK: 4 files" in out
    assert (tmp_path / "scratch").joinpath(*BOOK).is_file()


def test_a_whole_root_without_a_manifest_can_be_checked_too(root, tmp_path, capsys):
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK: 4 files" in out
    assert (tmp_path / "scratch").joinpath(*BOOK).is_file()


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
    out, err = capsys.readouterr()
    assert rc == 2
    assert "is not empty" in out
    assert "Traceback" not in err
    assert (scratch / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))  # source untouched


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
    out, err = capsys.readouterr()
    assert rc == 2
    assert "cannot read the copy" in out and "is not a directory" in out
    assert "Traceback" not in err


def test_manifest_with_truncated_json_returns_2(day_dir, tmp_path, capsys):
    (day_dir / MANIFEST_NAME).write_text('{"incomplete":', encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "cannot read the copy" in out
    assert "Traceback" not in err


def test_scratch_that_is_a_file_returns_2(day_dir, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    scratch.write_text("i am a file", encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "is not a directory" in out
    assert "Traceback" not in err


def test_manifest_not_a_dict_returns_2(day_dir, tmp_path, capsys):
    (day_dir / MANIFEST_NAME).write_text("[]", encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "cannot read the copy" in out
    assert "Traceback" not in err


def test_scratch_parent_is_file_returns_2(day_dir, tmp_path, capsys):
    afile = tmp_path / "afile"
    afile.write_text("i am a file", encoding="utf-8")
    scratch_path = str(afile / "sub")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", scratch_path])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "cannot prepare the scratch root" in out
    assert "Traceback" not in err


def test_scratch_exists_but_not_directory_returns_2(tmp_path, capsys):
    root = tmp_path / "quant_data"
    book = root.joinpath(*BOOK)
    book.parent.mkdir(parents=True)
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE positions (question_id TEXT, size REAL)")
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT)")
        conn.commit()
    scratch = tmp_path / "scratch"
    scratch.write_text("i am a file", encoding="utf-8")
    rc = restore_check.main(["--from", str(root), "--scratch", str(scratch)])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "is not a directory" in out
    assert "Traceback" not in err


def test_large_book_with_integrity_check_failure(root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    with closing(sqlite3.connect(book)) as conn:
        for i in range(10000):
            conn.execute("INSERT INTO positions VALUES (?, ?)", (f"q{i}", 1.5))
        conn.commit()

    with closing(sqlite3.connect(book)) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]

    data = bytearray(book.read_bytes())
    file_size = len(data)
    if file_size > page_size * 6:
        corrupt_offset = page_size * 5 + 100
        data[corrupt_offset:corrupt_offset + 200] = b'\x00' * 200
        book.write_bytes(bytes(data))

    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db" in out
    assert "integrity check" in out


def test_json_content_failure(root, tmp_path, capsys):
    model = root.joinpath(*MODEL)
    model.write_text("{broken", encoding="utf-8")
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL climate_data/models/gtm_emos.json" in out
    assert "not JSON" in out


def test_multiple_content_failures_all_reported(root, tmp_path, capsys):
    root.joinpath(*RUNS).write_bytes(b'{"ok": true}\n\xff\xfe\n')
    permid = root / "ibkr_ticks" / "paper" / "permid_map.json"
    permid.parent.mkdir(parents=True, exist_ok=True)
    permid.write_text("{broken", encoding="utf-8")
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl" in out
    assert "FAIL ibkr_ticks/paper/permid_map.json" in out


def test_manifest_key_with_path_escape_fails(day_dir, tmp_path, capsys):
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["../escape.txt"] = {"sha256": "abc123", "size": 11}
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (day_dir / ".." / "escape.txt").parent.mkdir(exist_ok=True)
    (day_dir / ".." / "escape.txt").write_text("escape file", encoding="utf-8")
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ../escape.txt: path escapes the scratch root" in out
    assert not (scratch.parent / "escape.txt").exists()


def test_manifest_key_with_scratch_evil_escape_fails(day_dir, tmp_path, capsys):
    # plant the source ONLY at backups/scratch_evil/x.txt (day_dir is tmp_path/backups/20261020):
    # the key then restores to tmp_path/scratch_evil/x.txt, which shares the string prefix of
    # tmp_path/scratch (a startswith() check would accept it) but lies outside it
    backups = day_dir.parent
    scratch_evil_dir = backups / "scratch_evil"
    scratch_evil_file = scratch_evil_dir / "x.txt"
    scratch_evil_dir.mkdir()
    scratch_evil_file.write_text("evil", encoding="utf-8")

    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["../scratch_evil/x.txt"] = file_record(scratch_evil_file)
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ../scratch_evil/x.txt: path escapes the scratch root" in out
    assert not (tmp_path / "scratch_evil").exists()


def test_os_error_during_restore_prints_collected_failures(root, tmp_path, capsys):
    # the failures collected before the abort are printed ahead of the error line
    # Break the parquet file (it sorts first)
    root.joinpath(*LEDGER).write_bytes(b"not parquet")

    scratch = tmp_path / "scratch"
    original_copy2 = shutil.copy2

    with patch('restore_check.shutil.copy2') as mock_copy2:
        def side_effect(src, dst):
            if str(dst).endswith('.db'):
                raise PermissionError("denied")
            # Call the real copy2 for other files
            original_copy2(src, dst)
        mock_copy2.side_effect = side_effect

        rc = restore_check.main(["--from", str(root), "--scratch", str(scratch)])

    out, err = capsys.readouterr()
    assert rc == 2
    # The parquet FAIL should come before the cannot read line
    parquet_idx = out.index("FAIL climate_data/labels/labels_ledger.parquet")
    error_idx = out.index("cannot read the copy")
    assert parquet_idx < error_idx
    assert "PermissionError" in out
    assert "Traceback" not in err


def test_manifest_record_malformed_returns_2(day_dir, tmp_path, capsys):
    # a manifest entry that is not a {sha256, size} record makes the copy unreadable (exit 2)
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["oops"] = "not a record"
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out, err = capsys.readouterr()
    assert rc == 2
    assert "cannot read the copy" in out
    assert "malformed" in out
    assert "Traceback" not in err


# ---- the paper book is named, not the first entry of MANIFEST_FILES ----


def test_the_paper_book_key_is_the_book_file():
    assert restore_check.PAPER_DB_KEY == "ibkr_ticks/paper/ibkr_paper.db"
    assert restore_check.PAPER_DB_KEY in {"/".join(parts) for parts in spark_parity.MANIFEST_FILES}


REAL_ORDER = spark_parity.MANIFEST_FILES


@pytest.fixture
def book_last():
    """analysis.spark_parity.MANIFEST_FILES in another order (the sync ledger first, the paper
    book last), and restore_check imported again under it: its book key is made at import."""
    rest = [parts for parts in REAL_ORDER if parts not in (BOOK, RUNS)]
    reordered = (RUNS, *rest, BOOK)
    with pytest.MonkeyPatch.context() as patch_:
        patch_.setattr(spark_parity, "MANIFEST_FILES", reordered)
        importlib.reload(restore_check)
        yield reordered
    importlib.reload(restore_check)  # the patch is undone: back to the real order


def test_a_reordered_manifest_still_requires_the_paper_book(book_last, root, tmp_path, capsys):
    # Reading the book as MANIFEST_FILES[0] would now require the sync ledger, which this root
    # holds, and report RESTORE OK for a root with no book at all.
    assert book_last[0] == RUNS and root.joinpath(*RUNS).is_file()
    assert restore_check.MANIFEST_FILES == book_last  # restore_check is under the new order
    assert restore_check.PAPER_DB_KEY == "ibkr_ticks/paper/ibkr_paper.db"
    root.joinpath(*BOOK).unlink()
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: the copy has no paper book" in out
    assert "RESTORE OK" not in out


def test_the_real_order_is_back_once_the_reordered_one_is_done(root, tmp_path, capsys):
    # the fixture above reloads the module on its way out: a later test sees the real thing
    assert spark_parity.MANIFEST_FILES == REAL_ORDER
    assert restore_check.MANIFEST_FILES == REAL_ORDER
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    assert rc == 0, capsys.readouterr().out


# ---- a book torn mid-transaction, and the journal that makes it whole again ----


class Torn(NamedTuple):
    before: bytes   # the book as the last commit left it
    torn: bytes     # the book as a stop in the middle of the next transaction left it
    journal: bytes  # the hot rollback journal of that transaction


def _integrity(book: Path) -> str:
    """The first line of SQLite's integrity check of `book`, opened read-only."""
    with closing(sqlite3.connect(f"file:{book.as_posix()}?mode=ro", uri=True)) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _rows(book: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(book)) as conn:
        return conn.execute(sql).fetchall()


@pytest.fixture
def torn(tmp_path) -> Torn:
    """A paper book of 300 rows, and the same book as a stop in the middle of a transaction
    leaves it. The transaction rewrites every row through a ten-page cache, so pages of its
    new state have been spilled into the book file when the book and its journal are read
    with the transaction still open: the two files a process killed there leaves behind."""
    book = tmp_path / "torn_source" / BOOK[-1]
    book.parent.mkdir()
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE positions (question_id TEXT, size REAL)")
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO trade_log VALUES ('a', 'open')")
        conn.executemany("INSERT INTO positions VALUES (?, ?)",
                         [(f"q{i:05d}-" + "x" * 40, 1.5) for i in range(300)])
        conn.commit()
    before = book.read_bytes()
    with closing(sqlite3.connect(book, isolation_level=None)) as writer:
        writer.execute("PRAGMA cache_size = 10")
        writer.execute("BEGIN")
        writer.execute("UPDATE positions SET question_id = question_id || ?, size = size + 1",
                       ("y" * 200,))
        tore = book.read_bytes()
        hot = book.with_name(book.name + "-journal").read_bytes()
        writer.execute("ROLLBACK")
    # The tests below mean nothing unless the book really is torn, and torn for good: the
    # book file alone is not a database that passes the check any more.
    assert hot and tore != before
    alone = tmp_path / "torn_alone" / BOOK[-1]
    alone.parent.mkdir()
    alone.write_bytes(tore)
    assert _integrity(alone) != "ok"
    return Torn(before, tore, hot)


def _place(root: Path, book: bytes, journal: bytes | None) -> Path:
    """Put `book` in the root in place of its paper book, with `journal` beside it if given."""
    path = root.joinpath(*BOOK)
    path.write_bytes(book)
    if journal is not None:
        path.with_name(path.name + "-journal").write_bytes(journal)
    return path


def test_a_book_torn_mid_transaction_is_made_whole_by_the_journal_beside_it(
        root, torn, tmp_path, capsys):
    # The kept root of a rollback: a stop that landed mid-transaction left the book torn, and
    # rollback.sh copied its hot journal along. SQLite rolls the journal back on the next
    # open that may write, so the restored copy is opened that way once before it is checked.
    book = _place(root, torn.torn, torn.journal)
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(root), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK: 4 files" in out
    restored = scratch.joinpath(*BOOK)
    assert _integrity(restored) == "ok"
    # the book as the last commit left it: the interrupted rewrite of every row is gone
    assert _rows(restored, "SELECT count(*), min(size), max(size) FROM positions") == [
        (300, 1.5, 1.5)]
    assert _rows(restored, "SELECT order_id FROM trade_log") == [("a",)]
    # the source is only read: the torn book and its journal are as they were
    assert book.read_bytes() == torn.torn
    assert book.with_name(book.name + "-journal").read_bytes() == torn.journal


def test_the_same_torn_book_without_its_journal_fails(root, torn, tmp_path, capsys):
    # The control of the test above: it is the journal that makes the book pass.
    _place(root, torn.torn, None)
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: integrity check says" in out
    assert "RESTORE OK" not in out


STALE_JOURNALS = {
    "a header SQLite has zeroed": lambda torn: b"\0" * 512,
    "an empty file": lambda torn: b"",
    "bytes that are no journal": lambda torn: b"this is not a journal " * 20,
    "a hot journal whose transaction never reached the book": lambda torn: torn.journal,
}


@pytest.mark.parametrize("kind", STALE_JOURNALS)
def test_a_stale_journal_beside_a_consistent_book_is_no_failure(
        kind, root, torn, tmp_path, capsys):
    # A journal SQLite ignores or discards, or rolls back onto a book it matches page for page:
    # carrying it along must not turn a good book into a FAIL. (A read-only open cannot deal
    # with the last two kinds: it stops on "attempt to write a readonly database".)
    _place(root, torn.before, STALE_JOURNALS[kind](torn))
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(root), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK: 4 files" in out
    restored = scratch.joinpath(*BOOK)
    assert _integrity(restored) == "ok"
    assert _rows(restored, "SELECT count(*), min(size), max(size) FROM positions") == [
        (300, 1.5, 1.5)]


def test_a_day_directory_book_that_no_longer_matches_its_record_fails_on_the_hash(
        day_dir, torn, tmp_path, capsys):
    # For a backup day directory the manifest decides: the backup wrote its book with SQLite's
    # backup API, nothing lies beside it, and a journal that turns up beside a book that is
    # not the one the manifest lists does not make that book pass.
    book = day_dir.joinpath(*BOOK)
    book.write_bytes(torn.torn)
    book.with_name(book.name + "-journal").write_bytes(torn.journal)
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: sha256 or size differs from the reference" in out
    assert "RESTORE OK" not in out


def test_the_book_is_hashed_as_copied_before_a_journal_beside_it_rolls_it_back(
        day_dir, torn, tmp_path, capsys):
    # The order inside the restore. The manifest's record here was taken from the consistent
    # book, while the day directory holds the torn book with its hot journal. Rolled back
    # first, the torn book would become byte for byte the book of the record and pass the
    # hash; compared first, as it is, it differs, and nothing is carried or rolled back.
    premise = tmp_path / "premise" / BOOK[-1]
    premise.parent.mkdir()
    premise.write_bytes(torn.torn)
    premise.with_name(premise.name + "-journal").write_bytes(torn.journal)
    with closing(sqlite3.connect(premise)) as conn:  # the open that rolls the journal back
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    assert premise.read_bytes() == torn.before  # so a roll-back first would hide the tear

    book = day_dir.joinpath(*BOOK)
    book.write_bytes(torn.before)
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["ibkr_ticks/paper/ibkr_paper.db"] = file_record(book)  # of the consistent book
    (day_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    book.write_bytes(torn.torn)
    book.with_name(book.name + "-journal").write_bytes(torn.journal)
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db: sha256 or size differs from the reference" in out
    assert "RESTORE OK" not in out
    restored = scratch.joinpath(*BOOK)
    assert not restored.with_name(restored.name + "-journal").exists()  # not carried along
    assert restored.read_bytes() == torn.torn  # and not rolled back: the book as it was copied


def test_a_commit_that_lives_only_in_the_wal_beside_the_book_is_in_the_restored_book(
        root, tmp_path, capsys):
    # A book in WAL mode keeps its newest commits in "<book>-wal" until a checkpoint; a restore
    # that left the WAL behind would pass the check and silently lack them.
    book = tmp_path / "wal_source" / BOOK[-1]
    book.parent.mkdir()
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE positions (question_id TEXT, size REAL)")
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO trade_log VALUES ('a', 'open')")
        conn.commit()
        conn.execute("PRAGMA journal_mode = WAL")
    with closing(sqlite3.connect(book, isolation_level=None)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO trade_log VALUES ('b', 'open')")  # committed into the WAL
        files = {suffix: book.with_name(book.name + suffix).read_bytes()
                 for suffix in ("", "-wal", "-shm")}
    assert files["-wal"]  # the last connection's close would have folded it into the book
    target = root.joinpath(*BOOK)
    for suffix, data in files.items():
        target.with_name(target.name + suffix).write_bytes(data)
    scratch = tmp_path / "scratch"
    rc = restore_check.main(["--from", str(root), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 0, out
    restored = scratch.joinpath(*BOOK)
    assert _integrity(restored) == "ok"
    assert _rows(restored, "SELECT order_id FROM trade_log ORDER BY order_id") == [
        ("a",), ("b",)]


@pytest.mark.parametrize("present", [("-journal", "-wal", "-shm"), ("-wal",), ()])
def test_the_files_beside_the_book_are_carried_when_they_exist_and_never_required(
        present, root, tmp_path, monkeypatch):
    target = root.joinpath(*BOOK)
    for suffix in present:
        target.with_name(target.name + suffix).write_bytes(f"beside the book {suffix}".encode())
    # judging the book is the business of the tests above; here the carried files stay as they are
    monkeypatch.setattr(restore_check, "_check_content", lambda key, path: None)
    scratch = tmp_path / "scratch"
    assert restore_check.restore_and_verify(root, scratch) == []
    restored = scratch.joinpath(*BOOK)
    for suffix in ("-journal", "-wal", "-shm"):
        side = restored.with_name(restored.name + suffix)
        if suffix in present:
            assert side.read_bytes() == f"beside the book {suffix}".encode()
        else:
            assert not side.exists()
