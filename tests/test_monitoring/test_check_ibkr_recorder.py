"""Tests for scripts/check_ibkr_recorder.py (recorder health probe)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.check_ibkr_recorder as probe


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_state(data_root: Path, age_minutes: float, rows: int = 2900,
                 sweep_seconds: float = 780.0, universe_size: int = 2900) -> Path:
    """Heartbeat state file whose ts is ``age_minutes`` in the past."""
    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "recorder_state.json"
    path.write_text(json.dumps({
        "ts": (_now() - timedelta(minutes=age_minutes)).isoformat(),
        "rows": rows,
        "sweep_seconds": sweep_seconds,
        "universe_size": universe_size,
    }), encoding="utf-8")
    return path


def _write_events(data_root: Path, events: list[dict]) -> Path:
    path = data_root / "ibkr_events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _write_healthy_sync_runs(data_root: Path, age_hours: float = 1.0) -> Path:
    path = data_root / "paper" / "ibkr_sync_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_utc": (_now() - timedelta(hours=age_hours)).isoformat(),
        "ok": True,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _argv(state: Path, events: Path, extra: list[str] | None = None) -> list[str]:
    data_root = state.parent.parent
    sync_runs = _write_healthy_sync_runs(data_root)
    argv = [
        "--state-path", str(state),
        "--events-path", str(events),
        "--sync-runs-path", str(sync_runs),
        "--health-log-path", str(data_root / "ibkr_health.log"),
        # Never the real logs/ibkr_health_alert_state.json: every run writes it.
        "--alert-state-path", str(data_root / "ibkr_health_alert_state.json"),
    ]
    return argv + (extra or [])


@pytest.fixture
def alerts(monkeypatch) -> list[str]:
    """Record send_alert calls made by the probe module."""
    calls: list[str] = []

    def _record(message, method=None):
        calls.append(message)
        return True

    monkeypatch.setattr(probe, "send_alert", _record)
    return calls


# State file: fresh / stale / missing / corrupt


def test_fresh_state_ok(tmp_path, alerts, capsys):
    state = _write_state(tmp_path, age_minutes=5)
    # Two sweep files in today's UTC partition -> counted in the OK line.
    quotes = tmp_path / "quotes" / f"date={_now():%Y-%m-%d}"
    quotes.mkdir(parents=True)
    (quotes / "quotes-120000Z.parquet").write_bytes(b"")
    (quotes / "quotes-122000Z.parquet").write_bytes(b"")

    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl"))

    assert rc == 0
    assert alerts == []
    out = capsys.readouterr().out
    assert "OK" in out
    assert "parquet files today=2" in out
    assert out.isascii()


def test_fresh_state_ok_without_quotes_dir(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=5)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 0
    assert alerts == []


def test_stale_state_alerts_and_exits_1(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=120, rows=2874, sweep_seconds=791.4)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 1
    assert len(alerts) == 1
    assert "STALE" in alerts[0]
    assert "rows=2874" in alerts[0]
    assert "sweep_seconds=791.4" in alerts[0]
    assert "\n" not in alerts[0] and alerts[0].isascii()


def test_max_age_override_rescues_old_heartbeat(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=120)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl",
                          ["--quiet", "--max-age-minutes", "180"]))
    assert rc == 0
    assert alerts == []


def test_missing_state_is_critical(tmp_path, alerts):
    missing = tmp_path / "state" / "recorder_state.json"  # never written
    rc = probe.main(_argv(missing, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 1
    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]


def test_corrupt_state_is_critical(tmp_path, alerts):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    state = state_dir / "recorder_state.json"
    state.write_text("{not json", encoding="utf-8")
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 1
    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0]


# Events JSONL: unexpected disconnect vs recovery


def test_last_event_unexpected_disconnect_is_down(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=5)  # heartbeat still fresh
    events = _write_events(tmp_path, [
        {"ts": (_now() - timedelta(hours=3)).isoformat(), "event": "connected"},
        {"ts": (_now() - timedelta(minutes=30)).isoformat(), "event": "unexpected_disconnect"},
    ])
    rc = probe.main(_argv(state, events, ["--quiet"]))
    assert rc == 1
    assert len(alerts) == 1
    assert "DOWN" in alerts[0]
    assert "30 min" in alerts[0]  # downtime so far


def test_reconnect_after_disconnect_is_ok(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=5)
    events = _write_events(tmp_path, [
        {"ts": (_now() - timedelta(minutes=40)).isoformat(), "event": "unexpected_disconnect"},
        {"ts": (_now() - timedelta(minutes=35)).isoformat(), "event": "reconnected"},
    ])
    rc = probe.main(_argv(state, events, ["--quiet"]))
    assert rc == 0
    assert alerts == []


def test_expected_disconnect_is_not_down(tmp_path, alerts):
    """A planned stop (expected flag) must not page anyone."""
    state = _write_state(tmp_path, age_minutes=5)
    events = _write_events(tmp_path, [
        {"ts": (_now() - timedelta(minutes=10)).isoformat(),
         "event": "disconnect", "expected": True},
    ])
    rc = probe.main(_argv(state, events, ["--quiet"]))
    assert rc == 0
    assert alerts == []


def test_down_outranks_stale(tmp_path, alerts):
    """Auto-logoff signature: stale heartbeat AND a trailing disconnect event."""
    state = _write_state(tmp_path, age_minutes=900)
    events = _write_events(tmp_path, [
        {"ts": (_now() - timedelta(minutes=900)).isoformat(), "event": "unexpected_disconnect"},
    ])
    rc = probe.main(_argv(state, events, ["--quiet"]))
    assert rc == 1
    assert len(alerts) == 1
    assert "DOWN" in alerts[0]


# Alert failure handling + output control


def test_send_alert_raising_does_not_mask_exit_code(tmp_path, monkeypatch):
    def boom(message, method=None):
        raise RuntimeError("telegram exploded")

    monkeypatch.setattr(probe, "send_alert", boom)
    state = _write_state(tmp_path, age_minutes=120)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 1  # unhealthy exit preserved, no exception escapes


def test_send_alert_failure_note_never_echoes_the_exception_text(tmp_path, monkeypatch, capsys):
    fake_token = "123456789:FAKE-test-token-NOT-A-REAL-SECRET"

    def boom(message, method=None):
        raise RuntimeError(f"Max retries exceeded with url: /bot{fake_token}/sendMessage")

    monkeypatch.setattr(probe, "send_alert", boom)
    state = _write_state(tmp_path, age_minutes=120)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl"))  # NOT quiet
    assert rc == 1
    err = capsys.readouterr().err
    assert "send_alert failed (RuntimeError)" in err
    assert fake_token not in err


def test_quiet_suppresses_stdout(tmp_path, alerts, capsys):
    state = _write_state(tmp_path, age_minutes=120)
    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl", ["--quiet"]))
    assert rc == 1
    assert capsys.readouterr().out == ""
    assert len(alerts) == 1  # quiet silences stdout, never the alert


def test_gateway_schema_disconnect_is_down(tmp_path, alerts):
    """The gateway writes ConnectionEvent dicts (kind/ts_utc, not event/ts)."""
    state = _write_state(tmp_path, age_minutes=5)  # heartbeat fresh: DOWN must win
    events = _write_events(tmp_path, [{
        "kind": "disconnect",
        "ts_utc": (_now() - timedelta(minutes=45)).isoformat(),
        "detail": "heartbeat miss",
        "expected": False,
        "downtime_seconds": None,
    }])
    rc = probe.main(_argv(state, events))
    assert rc == 1
    assert "DOWN" in alerts[0]
    assert "45 min" in alerts[0]


def test_gateway_schema_gave_up_window_is_down(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=5)
    events = _write_events(tmp_path, [{
        "kind": "gave_up_window",
        "ts_utc": (_now() - timedelta(minutes=90)).isoformat(),
        "detail": "still down after restart window",
        "expected": False,
        "downtime_seconds": 5400.0,
    }])
    rc = probe.main(_argv(state, events))
    assert rc == 1
    assert "DOWN" in alerts[0]


def test_gateway_schema_reconnect_clears_down(tmp_path, alerts):
    state = _write_state(tmp_path, age_minutes=5)
    events = _write_events(tmp_path, [
        {"kind": "disconnect", "ts_utc": _now().isoformat(), "expected": False},
        {"kind": "reconnect", "ts_utc": _now().isoformat(), "expected": False,
         "downtime_seconds": 120.0},
    ])
    rc = probe.main(_argv(state, events))
    assert rc == 0
    assert alerts == []


def test_ok_message_includes_snapshot_age_days(tmp_path, alerts, capsys):
    state = _write_state(tmp_path, age_minutes=5)
    snapshot = tmp_path / "universe.json"
    snapshot.write_text("{}", encoding="utf-8")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(snapshot, (ten_days_ago, ten_days_ago))

    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl",
                          ["--snapshot-path", str(snapshot)]))

    assert rc == 0
    out = capsys.readouterr().out
    assert "snapshot_age_days=10" in out


def test_snapshot_age_days_is_na_when_file_missing(tmp_path, alerts, capsys):
    state = _write_state(tmp_path, age_minutes=5)
    missing_snapshot = tmp_path / "does_not_exist.json"

    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl",
                          ["--snapshot-path", str(missing_snapshot)]))

    assert rc == 0
    out = capsys.readouterr().out
    assert "snapshot_age_days=n/a" in out


def test_stale_snapshot_does_not_flip_status_or_exit_code(tmp_path, alerts, capsys):
    state = _write_state(tmp_path, age_minutes=5)
    snapshot = tmp_path / "universe.json"
    snapshot.write_text("{}", encoding="utf-8")
    ancient = time.time() - 400 * 86400
    os.utime(snapshot, (ancient, ancient))

    rc = probe.main(_argv(state, tmp_path / "ibkr_events.jsonl",
                          ["--snapshot-path", str(snapshot)]))

    assert rc == 0
    assert alerts == []
    out = capsys.readouterr().out
    assert "ibkr recorder OK" in out
    assert "snapshot_age_days=400" in out
