"""Task 1.6: the DB lockfile (`src/trading/locks.py`) and the two pure
helpers behind `scripts/sync_ibkr_paper.py`.

The entrypoint's venue-touching `main()` is exercised by the operator in
Task 1.9 -- no test here connects anywhere. What IS unit-tested, per the
task brief, are its two pure helpers: the jsonl record builder (from a
`ReconcileReport`) and the report summary formatter. They are kept as
module-level functions in the script itself rather than moved into
`src/trading/reconcile.py` -- this repo already imports script modules as a
namespace package in tests (see
`tests/test_trading/test_migrate_ibkr_paper_v2.py`'s
`from scripts.migrations.migrate_ibkr_paper_v2 import migrate`), so no new
machinery is needed, and `reconcile.py` (owned by Task 1.5, out of this
task's allowed-files set except for this one permitted option) stays
untouched.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.sync_ibkr_paper import build_jsonl_record, format_report
from src.trading.locks import LockHeldError, acquire_db_lock
from src.trading.reconcile import ReconcileReport


def _lock_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".lock")


# ------------------------------------------------------------------ locks
def test_concurrent_acquisition_raises(tmp_path):
    db = tmp_path / "book.db"
    with acquire_db_lock(db):
        with pytest.raises(LockHeldError):
            with acquire_db_lock(db):
                pass  # pragma: no cover -- must never get here


def test_concurrent_acquisition_error_names_the_holder(tmp_path):
    db = tmp_path / "book.db"
    with acquire_db_lock(db):
        with pytest.raises(LockHeldError, match=str(os.getpid())):
            with acquire_db_lock(db):
                pass  # pragma: no cover


def test_stale_lock_is_broken_with_a_warning_and_acquired(tmp_path, caplog):
    db = tmp_path / "book.db"
    lock_path = _lock_path(db)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    lock_path.write_text(json.dumps({"pid": 999999, "ts_utc": stale_ts}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="src.trading.locks"):
        with acquire_db_lock(db, stale_after_min=10.0):
            held = json.loads(lock_path.read_text(encoding="utf-8"))
            assert held["pid"] == os.getpid()

    assert any("stale" in rec.message.lower() for rec in caplog.records)
    assert not lock_path.exists()  # released cleanly once the stale lock was ours


def test_a_fresh_lock_is_not_broken_even_if_requested_stale_window_is_tiny(tmp_path):
    # A lock written "now" must not be treated as stale just because the
    # caller passes a very small stale_after_min -- age is computed from the
    # real wall clock, not assumed.
    db = tmp_path / "book.db"
    with acquire_db_lock(db):
        with pytest.raises(LockHeldError):
            with acquire_db_lock(db, stale_after_min=0.0001):
                pass  # pragma: no cover


def test_released_on_clean_exit(tmp_path):
    db = tmp_path / "book.db"
    lock_path = _lock_path(db)
    with acquire_db_lock(db):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_released_on_exception(tmp_path):
    db = tmp_path / "book.db"
    lock_path = _lock_path(db)
    with pytest.raises(RuntimeError, match="boom"):
        with acquire_db_lock(db):
            assert lock_path.exists()
            raise RuntimeError("boom")
    assert not lock_path.exists()


def test_lock_replaced_by_another_process_is_not_deleted_on_exit(tmp_path):
    db = tmp_path / "book.db"
    lock_path = _lock_path(db)
    with acquire_db_lock(db):
        # Simulate another process breaking our lock as stale and taking it
        # over while we are still "running" (clock skew, or a run that
        # legitimately outran stale_after_min).
        other = {"pid": 424242, "ts_utc": datetime.now(timezone.utc).isoformat()}
        lock_path.write_text(json.dumps(other), encoding="utf-8")
    # Our exit must not have deleted the other process's lock.
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8")) == other


def test_missing_lock_file_at_release_is_tolerated(tmp_path):
    db = tmp_path / "book.db"
    lock_path = _lock_path(db)
    with acquire_db_lock(db):
        lock_path.unlink()  # someone else already cleaned it up
    assert not lock_path.exists()  # no error raised getting here


# ---------------------------------------------------------- entrypoint helpers
def _report(**overrides) -> ReconcileReport:
    fields = dict(examined=3, filled=1, partially_filled=1, cancelled=0, unchanged=1,
                  unmatched_venue_orders=2, notes=["note one", "note two"], aborted=False)
    fields.update(overrides)
    return ReconcileReport(**fields)


def test_build_jsonl_record_carries_every_contract_field():
    rep = _report()
    ts = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    record = build_jsonl_record(rep, account="DU123456", db="C:/x/ibkr_paper.db", ts=ts)
    assert record == {
        "ts_utc": ts.isoformat(),
        "ok": True,
        "aborted": False,
        "examined": 3,
        "filled": 1,
        "partially_filled": 1,
        "cancelled": 0,
        "unchanged": 1,
        "unmatched_venue_orders": 2,
        "notes": ["note one", "note two"],
        "account": "DU123456",
        "db": "C:/x/ibkr_paper.db",
    }


def test_build_jsonl_record_ok_false_when_aborted():
    rep = _report(aborted=True, notes=["ABORTED, nothing written: some reason"])
    record = build_jsonl_record(rep, account="DU1", db="db.db")
    assert record["ok"] is False
    assert record["aborted"] is True


def test_build_jsonl_record_ok_can_be_forced_false_for_the_exception_path():
    rep = ReconcileReport(notes=["EXCEPTION: gateway dropped mid-sync"])
    record = build_jsonl_record(rep, account="", db="db.db", ok=False)
    assert record["ok"] is False
    assert record["aborted"] is False  # the report itself never claimed a guard fired
    assert record["examined"] == 0


def test_build_jsonl_record_is_json_serializable():
    rep = _report()
    record = build_jsonl_record(rep, account="DU1", db="db.db")
    json.dumps(record)  # must not raise


def test_format_report_includes_counters_and_every_note():
    rep = _report()
    text = format_report(rep)
    assert "examined=3" in text
    assert "filled=1" in text
    assert "partially_filled=1" in text
    assert "cancelled=0" in text
    assert "unchanged=1" in text
    assert "unmatched_venue_orders=2" in text
    assert "note one" in text
    assert "note two" in text


def test_format_report_notes_aborted_state():
    rep = _report(aborted=True)
    assert "aborted=True" in format_report(rep)


def test_format_report_on_a_clean_report_has_no_note_lines():
    rep = _report(notes=[])
    text = format_report(rep)
    assert text.strip().count("\n") == 0  # only the counters line, no bullets
