"""monitoring/backup_paper_book.py: a daily, consistent, rotated copy of the operational files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

from analysis.spark_parity import MANIFEST_FILES
from monitoring.backup_paper_book import KEEP_DAYS, MANIFEST_NAME, main, run_backup

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
    # closing(): a connection that is only left behind keeps the file locked on Windows
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
    # an operational file this root does not hold is recorded as absent, not skipped
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
