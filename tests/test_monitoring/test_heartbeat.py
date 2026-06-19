"""Tests for monitoring.heartbeat staleness + growth + alerting logic."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from monitoring.heartbeat import run_heartbeat, send_alert


NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


def _make_db(tmp_path: Path, newest_age_minutes: float, n_rows: int = 3) -> Path:
    """Build a synthetic snapshots DB whose newest row is ``newest_age_minutes`` old."""
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, midpoint REAL)"
    )
    newest = NOW - timedelta(minutes=newest_age_minutes)
    for i in range(n_rows):
        ts = (newest - timedelta(minutes=i)).isoformat()
        conn.execute(
            "INSERT INTO snapshots(market_id, timestamp, midpoint) VALUES (?, ?, ?)",
            (f"m{i}", ts, 0.5),
        )
    conn.commit()
    conn.close()
    return db


def _make_fresh_logs(tmp_path: Path, now: datetime = NOW) -> Path:
    """Create collector logs with a recent success marker so check 3 passes."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    mkt_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z") or \
        (now - timedelta(minutes=5)).isoformat()
    (log_dir / "market_collector.log").write_text(
        f"{(now - timedelta(minutes=5)).isoformat()} [INFO] collect_markets: "
        f"Collection run finished with exit code 0\n",
        encoding="utf-8",
    )
    (log_dir / "snapshot_collector.log").write_text(
        f"{(now - timedelta(minutes=5)).isoformat()} [INFO] collect_snapshots: "
        f"Run summary: total=10 stored=10\n",
        encoding="utf-8",
    )
    return log_dir


def test_fresh_db_passes(tmp_path):
    db = _make_db(tmp_path, newest_age_minutes=5)
    log_dir = _make_fresh_logs(tmp_path)
    state = tmp_path / "state.json"
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert failures == [], f"expected healthy, got: {failures}"


def test_stale_db_flags_staleness(tmp_path):
    db = _make_db(tmp_path, newest_age_minutes=90)  # 90 min > 45 threshold
    log_dir = _make_fresh_logs(tmp_path)
    state = tmp_path / "state.json"
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert any("stale" in f.lower() for f in failures), failures


def test_no_growth_flagged_after_two_consecutive(tmp_path):
    db = _make_db(tmp_path, newest_age_minutes=5, n_rows=3)
    log_dir = _make_fresh_logs(tmp_path)
    state = tmp_path / "state.json"
    # Pre-seed: same count (3) AND a prior no-growth streak of 1, so this run
    # makes it 2 consecutive → flag.
    state.write_text(json.dumps({
        "last_count": 3, "last_checked": "2026-06-19T11:30:00+00:00",
        "consecutive_no_growth": 1,
    }), encoding="utf-8")
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert any("did not grow" in f for f in failures), failures
    rewritten = json.loads(state.read_text(encoding="utf-8"))
    assert rewritten["last_count"] == 3
    assert rewritten["last_checked"] == NOW.isoformat()
    assert rewritten["consecutive_no_growth"] == 2


def test_single_no_growth_not_flagged(tmp_path):
    """A single no-growth observation must NOT alert (cadence misalignment)."""
    db = _make_db(tmp_path, newest_age_minutes=5, n_rows=3)
    log_dir = _make_fresh_logs(tmp_path)
    state = tmp_path / "state.json"
    # Same count, but no prior streak → first no-growth → no flag yet.
    state.write_text(json.dumps({"last_count": 3, "last_checked": "2026-06-19T11:30:00+00:00"}),
                     encoding="utf-8")
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert not any("did not grow" in f for f in failures), failures
    rewritten = json.loads(state.read_text(encoding="utf-8"))
    assert rewritten["consecutive_no_growth"] == 1


def test_growth_passes(tmp_path):
    db = _make_db(tmp_path, newest_age_minutes=5, n_rows=5)
    log_dir = _make_fresh_logs(tmp_path)
    state = tmp_path / "state.json"
    # Previous count lower than current (5) → growth, no flag.
    state.write_text(json.dumps({"last_count": 3, "last_checked": "2026-06-19T11:30:00+00:00"}),
                     encoding="utf-8")
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert not any("did not grow" in f for f in failures), failures


def test_missing_collector_log_flagged(tmp_path):
    db = _make_db(tmp_path, newest_age_minutes=5)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()  # empty — no collector logs
    state = tmp_path / "state.json"
    failures = run_heartbeat(
        db_path=db, state_path=state, log_dir=log_dir,
        max_age_minutes=45, now=NOW,
    )
    assert any("collect_markets" in f for f in failures), failures
    assert any("collect_snapshots" in f for f in failures), failures


def test_send_alert_log_only_emits_critical(caplog):
    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        send_alert("test message", method="log_only")
    assert any("test message" in r.message for r in caplog.records)


def test_send_alert_telegram_missing_creds_falls_back(caplog, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        send_alert("fallback message", method="telegram")
    # No creds → must fall back to a CRITICAL log line, not crash.
    assert any("fallback message" in r.message for r in caplog.records)
