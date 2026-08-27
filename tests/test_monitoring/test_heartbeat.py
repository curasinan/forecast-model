"""Tests for monitoring.heartbeat: the shared alert dispatcher.

The Polymarket-era checks this module used to run (snapshot freshness,
snapshot growth, collector-log success-in-window against
data/polymarket.db) were deleted with the checks themselves 2026-08-27 --
that pipeline is retired (docs/MIGRATION_CHECKPOINT.md) and their tests went
with them. What remains and is tested here: send_alert (the dispatcher
scripts/check_ibkr_recorder.py imports and relies on) and the retired CLI's
loud pointer.
"""

from __future__ import annotations

import logging

from monitoring.heartbeat import main, send_alert


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


def test_retired_main_points_to_probe_and_exits_0(capsys):
    """python -m monitoring.heartbeat must not silently claim success -- it
    prints a pointer to the live probe and exits 0 (retired, not broken)."""
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "check_ibkr_recorder.py" in out
    assert "retired" in out.lower()
