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

import pytest
import requests

import monitoring.heartbeat as heartbeat_mod
from monitoring.heartbeat import main, send_alert

# Obviously-fake Telegram credentials -- the shape of a real bot token
# (<digits>:<secret>) so a leak is detectable, but no real secret anywhere.
FAKE_TOKEN = "123456789:FAKE-test-token-NOT-A-REAL-SECRET"
FAKE_CHAT_ID = "-1000000000000"


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


# --------------------------------------------------------------------------- #
# C-2 (final review): the bot token lives in the Telegram URL PATH
# (/bot<TOKEN>/sendMessage), and requests' connection-error text quotes that
# path verbatim ("Max retries exceeded with url: /bot<TOKEN>/sendMessage").
# The health probe's stderr is appended to logs/ibkr_health_task.log
# (scripts/scheduling/run_ibkr_health.ps1, 2>&1) -- hourly during exactly the
# outages that trigger this path. Guard-catches-known-bad: the leaking
# exception text is fed in deliberately and the token must appear NOWHERE.
# --------------------------------------------------------------------------- #


def _leaky_text() -> str:
    return (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: /bot{FAKE_TOKEN}/sendMessage (Caused by NewConnectionError("
        "'<urllib3.connection.HTTPSConnection object>: Failed to establish a new "
        "connection: [Errno 11001] getaddrinfo failed'))"
    )


def _all_logged_text(caplog) -> str:
    """Every rendered log line PLUS every record's formatted exc_info/stack,
    so a traceback-carried leak is caught too, not just the message."""
    parts = [caplog.text]
    formatter = logging.Formatter()
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(formatter.formatException(record.exc_info))
        if record.stack_info:
            parts.append(record.stack_info)
    return "\n".join(parts)


@pytest.mark.parametrize(
    "exc_type",
    [requests.ConnectionError, requests.exceptions.ConnectTimeout, requests.exceptions.SSLError],
)
def test_telegram_send_error_never_logs_the_bot_token(caplog, capsys, monkeypatch, exc_type):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    def _raise(*args, **kwargs):
        raise exc_type(_leaky_text())

    monkeypatch.setattr(heartbeat_mod.requests, "post", _raise)

    # Sanity: the fixture really does carry the token (otherwise the
    # absence assertions below would pass vacuously).
    assert FAKE_TOKEN in _leaky_text()

    with caplog.at_level(logging.DEBUG):
        send_alert("probe says DOWN", method="telegram")

    logged = _all_logged_text(caplog)
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in logged
    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err
    # The secret half alone must not leak either (e.g. a partial redaction).
    assert FAKE_TOKEN.split(":", 1)[1] not in logged
    # The alert itself still lands, as a CRITICAL fallback line, naming the
    # failure class so an operator can still diagnose it.
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "the CRITICAL fallback must still fire"
    assert any("probe says DOWN" in r.getMessage() for r in critical)
    assert any(exc_type.__name__ in r.getMessage() for r in critical)


def test_telegram_unexpected_exception_is_also_contained_without_the_token(
    caplog, capsys, monkeypatch
):
    """A non-RequestException carrying the URL (e.g. an encoding error raised
    while building the request) must not escape send_alert either: the probe
    (scripts/check_ibkr_recorder.py) would otherwise print it to stderr --
    the same task-log channel."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    def _raise(*args, **kwargs):
        raise ValueError(f"bad url https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")

    monkeypatch.setattr(heartbeat_mod.requests, "post", _raise)

    with caplog.at_level(logging.DEBUG):
        send_alert("probe says STALE", method="telegram")  # must not raise

    logged = _all_logged_text(caplog)
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in logged
    assert FAKE_TOKEN not in captured.err
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any("probe says STALE" in r.getMessage() for r in critical)


def test_telegram_alert_message_containing_the_token_is_redacted(caplog, monkeypatch):
    """Belt: even if the token somehow reached the alert text itself, the
    fallback line scrubs it."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    def _raise(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(heartbeat_mod.requests, "post", _raise)

    with caplog.at_level(logging.DEBUG):
        send_alert(f"weird alert mentioning {FAKE_TOKEN}", method="telegram")

    assert FAKE_TOKEN not in _all_logged_text(caplog)
    assert any("weird alert mentioning" in r.getMessage() for r in caplog.records)


def test_retired_main_points_to_probe_and_exits_0(capsys):
    """python -m monitoring.heartbeat must not silently claim success -- it
    prints a pointer to the live probe and exits 0 (retired, not broken)."""
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "check_ibkr_recorder.py" in out
    assert "retired" in out.lower()
