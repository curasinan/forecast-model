"""Tests for the durable health-alert sink, the DEGRADED check and paper-sync freshness."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.check_ibkr_recorder as probe


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_state(data_root: Path, age_minutes: float, rows: int = 2900,
                 sweep_seconds: float = 780.0, universe_size: int | None = 2900) -> Path:
    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "recorder_state.json"
    record = {
        "ts": (_now() - timedelta(minutes=age_minutes)).isoformat(),
        "rows": rows,
        "sweep_seconds": sweep_seconds,
    }
    if universe_size is not None:
        record["universe_size"] = universe_size
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _write_sync_runs_raw(data_root: Path, text: str) -> Path:
    path = data_root / "paper" / "ibkr_sync_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sync_line(*, ok: bool, age_hours: float) -> str:
    return json.dumps({
        "ts_utc": (_now() - timedelta(hours=age_hours)).isoformat(),
        "ok": ok,
    })


def _healthy_sync_runs(data_root: Path, age_hours: float = 1.0) -> Path:
    return _write_sync_runs_raw(data_root, _sync_line(ok=True, age_hours=age_hours) + "\n")


def _argv(data_root: Path, *, extra: list[str] | None = None,
         health_log: Path | None = None) -> list[str]:
    return [
        "--state-path", str(data_root / "state" / "recorder_state.json"),
        "--events-path", str(data_root / "ibkr_events.jsonl"),
        "--sync-runs-path", str(data_root / "paper" / "ibkr_sync_runs.jsonl"),
        "--health-log-path", str(health_log or (data_root / "ibkr_health.log")),
        # Never the real logs/ibkr_health_alert_state.json: every run writes it.
        "--alert-state-path", str(data_root / "ibkr_health_alert_state.json"),
        "--quiet",
    ] + (extra or [])


def _status_lines(health_log: Path) -> list[str]:
    """The durable log's per-run status lines."""
    lines = health_log.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if " ALERT_" not in line]


@pytest.fixture
def alerts(monkeypatch) -> list[str]:
    """Record send_alert calls; answers True like a delivered alert."""
    calls: list[str] = []

    def _record(message, method=None):
        calls.append(message)
        return True

    monkeypatch.setattr(probe, "send_alert", _record)
    return calls


# Durable sink (H3): non-OK appends one line, OK appends nothing


def test_stale_appends_exactly_one_status_line_to_health_log(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=120)  # STALE (default threshold 60 min)
    _healthy_sync_runs(tmp_path)
    health_log = tmp_path / "ibkr_health.log"

    rc = probe.main(_argv(tmp_path, health_log=health_log))

    assert rc == 1
    assert health_log.exists()
    lines = _status_lines(health_log)
    assert len(lines) == 1
    ts, status, rest = lines[0].split(" ", 2)
    assert status == "STALE"
    assert "STALE" in rest
    # Timestamp parses and is UTC-ISO.
    datetime.fromisoformat(ts)
    assert len(health_log.read_text(encoding="utf-8").splitlines()) == 2


def test_ok_appends_nothing_to_health_log(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)  # fresh, rows=2900/universe=2900 -> OK
    _healthy_sync_runs(tmp_path)
    health_log = tmp_path / "ibkr_health.log"

    rc = probe.main(_argv(tmp_path, health_log=health_log))

    assert rc == 0
    assert alerts == []
    assert not health_log.exists()


def test_two_consecutive_failures_append_two_status_lines(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=120)
    _healthy_sync_runs(tmp_path)
    health_log = tmp_path / "ibkr_health.log"

    probe.main(_argv(tmp_path, health_log=health_log))
    probe.main(_argv(tmp_path, health_log=health_log))

    assert len(_status_lines(health_log)) == 2
    assert len(alerts) == 1


def test_sink_raising_does_not_prevent_send_alert(tmp_path, alerts, monkeypatch, capsys):
    """A durable-sink write failure must not skip the Telegram attempt."""
    def boom(log_path, status, message, now=None):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(probe, "_append_health_log", boom)
    _write_state(tmp_path, age_minutes=120)  # STALE
    _healthy_sync_runs(tmp_path)
    argv = [
        "--state-path", str(tmp_path / "state" / "recorder_state.json"),
        "--events-path", str(tmp_path / "ibkr_events.jsonl"),
        "--sync-runs-path", str(tmp_path / "paper" / "ibkr_sync_runs.jsonl"),
        "--health-log-path", str(tmp_path / "ibkr_health.log"),
        "--alert-state-path", str(tmp_path / "ibkr_health_alert_state.json"),
    ]

    rc = probe.main(argv)

    assert rc == 1
    assert len(alerts) == 1
    assert "STALE" in alerts[0]
    err = capsys.readouterr().err
    assert "_append_health_log failed (OSError)" in err
    assert "permission denied" not in err


def test_send_alert_raising_still_writes_sink_line(tmp_path, monkeypatch):
    """The converse: a Telegram failure must not skip the durable sink write."""
    def boom(message, method=None):
        raise RuntimeError("telegram exploded")

    monkeypatch.setattr(probe, "send_alert", boom)
    _write_state(tmp_path, age_minutes=120)  # STALE
    _healthy_sync_runs(tmp_path)
    health_log = tmp_path / "ibkr_health.log"

    rc = probe.main(_argv(tmp_path, health_log=health_log))

    assert rc == 1  # unhealthy exit preserved, no exception escapes
    assert health_log.exists()
    lines = _status_lines(health_log)
    assert len(lines) == 1
    _, status, rest = lines[0].split(" ", 2)
    assert status == "STALE"
    assert "STALE" in rest


# M6: DEGRADED -- fresh heartbeat, thin data flow


def test_degraded_when_rows_below_half_universe(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5, rows=10, universe_size=2900)
    _healthy_sync_runs(tmp_path)

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert len(alerts) == 1
    assert "DEGRADED" in alerts[0]
    assert "rows=10" in alerts[0]
    assert "universe_size=2900" in alerts[0]


def test_no_degraded_when_universe_size_zero(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5, rows=10, universe_size=0)
    _healthy_sync_runs(tmp_path)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_no_degraded_when_universe_size_missing(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5, rows=10, universe_size=None)
    _healthy_sync_runs(tmp_path)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_rows_at_half_universe_is_not_degraded(tmp_path, alerts):
    """Boundary: exactly 50% is healthy (strict less-than triggers DEGRADED)."""
    _write_state(tmp_path, age_minutes=5, rows=1450, universe_size=2900)
    _healthy_sync_runs(tmp_path)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_stale_outranks_degraded(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=120, rows=10, universe_size=2900)
    _healthy_sync_runs(tmp_path)

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert "STALE" in alerts[0]
    assert "DEGRADED" not in alerts[0]


# Operator-mandated: SYNC_STALE (26h default)


def test_sync_stale_when_last_ok_27h_old(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)  # recorder healthy
    _healthy_sync_runs(tmp_path, age_hours=27.0)

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert len(alerts) == 1
    assert "SYNC_STALE" in alerts[0]


def test_sync_healthy_when_last_ok_25h_old(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)
    _healthy_sync_runs(tmp_path, age_hours=25.0)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_sync_stale_when_only_ok_false_lines(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)
    _write_sync_runs_raw(tmp_path, _sync_line(ok=False, age_hours=1.0) + "\n")

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert "SYNC_STALE" in alerts[0]
    assert "no successful run" in alerts[0]


def test_sync_stale_when_file_missing(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)
    # No sync-runs file written at all.

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert "SYNC_STALE" in alerts[0]
    assert "missing" in alerts[0]


def test_sync_malformed_line_is_skipped_not_fatal(tmp_path, alerts):
    text = "not json at all\n" + _sync_line(ok=True, age_hours=1.0) + "\n"
    _write_state(tmp_path, age_minutes=5)
    _write_sync_runs_raw(tmp_path, text)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_sync_malformed_trailing_line_does_not_mask_valid_success(tmp_path, alerts):
    text = (
        _sync_line(ok=True, age_hours=1.0) + "\n"
        + "{not json\n"
    )
    _write_state(tmp_path, age_minutes=5)
    _write_sync_runs_raw(tmp_path, text)

    rc = probe.main(_argv(tmp_path))

    assert rc == 0
    assert alerts == []


def test_max_sync_age_hours_override(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5)
    _healthy_sync_runs(tmp_path, age_hours=27.0)

    rc = probe.main(_argv(tmp_path, extra=["--max-sync-age-hours", "30"]))

    assert rc == 0
    assert alerts == []


# Precedence + combined message when both axes fail


def test_both_axes_failing_recorder_leads_message_mentions_both(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=120)  # STALE
    _healthy_sync_runs(tmp_path, age_hours=27.0)  # SYNC_STALE
    health_log = tmp_path / "ibkr_health.log"

    rc = probe.main(_argv(tmp_path, health_log=health_log))

    assert rc == 1
    assert len(alerts) == 1
    message = alerts[0]
    assert "STALE" in message
    assert "SYNC_STALE" in message
    assert message.index("ibkr recorder STALE") < message.index("ibkr paper sync SYNC_STALE")
    # The durable sink's status field is the recorder status, not SYNC_STALE.
    (line,) = _status_lines(health_log)
    _, status, _ = line.split(" ", 2)
    assert status == "STALE"


def test_degraded_and_sync_stale_both_mentioned(tmp_path, alerts):
    _write_state(tmp_path, age_minutes=5, rows=10, universe_size=2900)  # DEGRADED
    _healthy_sync_runs(tmp_path, age_hours=27.0)  # SYNC_STALE

    rc = probe.main(_argv(tmp_path))

    assert rc == 1
    assert "DEGRADED" in alerts[0]
    assert "SYNC_STALE" in alerts[0]


# Secrets rule: the Telegram token must never appear in any log line


def test_token_never_logged_with_misconfigured_telegram(tmp_path, monkeypatch, caplog):
    secret_token = "123456:AA_super_secret_token_value"
    monkeypatch.setenv("ALERT_METHOD", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret_token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")  # blank -> send_alert falls back, no network

    _write_state(tmp_path, age_minutes=120)  # STALE -> non-OK -> alert fires
    _healthy_sync_runs(tmp_path)
    health_log = tmp_path / "ibkr_health.log"

    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        rc = probe.main(_argv(tmp_path, health_log=health_log))

    assert rc == 1
    assert secret_token not in caplog.text
    assert secret_token not in health_log.read_text(encoding="utf-8")
