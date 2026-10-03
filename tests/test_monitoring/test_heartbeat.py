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


# --------------------------------------------------------------------------- #
# send_alert reports whether the alert was delivered. The health probe's alert
# state machine (scripts/check_ibkr_recorder.py) depends on it: an undelivered
# alert is retried on the next run instead of being marked as sent.
#   True  = delivered (Telegram HTTP 200, or the log_only method wrote its line)
#   False = not delivered (missing token/chat id, non-200, any exception)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _telegram_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)


def test_send_alert_log_only_returns_true_after_writing_its_line(caplog):
    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        outcome = send_alert("test message", method="log_only")
    assert outcome is True
    assert any("test message" in r.getMessage() for r in caplog.records)


def test_send_alert_default_method_is_log_only_and_returns_true(caplog, monkeypatch):
    monkeypatch.delenv("ALERT_METHOD", raising=False)
    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        outcome = send_alert("default message")
    assert outcome is True
    assert any("default message" in r.getMessage() for r in caplog.records)


def test_send_alert_telegram_http_200_returns_true(caplog, monkeypatch):
    _telegram_env(monkeypatch)
    posts: list[tuple[str, dict]] = []

    def _post(url, **kwargs):
        posts.append((url, kwargs))
        return _FakeResponse(200)

    monkeypatch.setattr(heartbeat_mod.requests, "post", _post)

    with caplog.at_level(logging.DEBUG):
        outcome = send_alert("probe says DOWN", method="telegram")

    assert outcome is True
    ((url, kwargs),) = posts
    assert url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert kwargs["json"] == {"chat_id": FAKE_CHAT_ID, "text": "probe says DOWN"}
    # Delivered: no fallback line, and the token (which the URL must carry)
    # still never reaches a log line.
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert FAKE_TOKEN not in _all_logged_text(caplog)


def test_send_alert_env_selected_telegram_returns_true(monkeypatch):
    """The probe calls ``send_alert(message)`` with no method: ALERT_METHOD
    from the environment picks Telegram, and a 200 is reported as delivered."""
    _telegram_env(monkeypatch)
    monkeypatch.setenv("ALERT_METHOD", "telegram")
    monkeypatch.setattr(heartbeat_mod.requests, "post", lambda url, **kw: _FakeResponse(200))

    assert send_alert("probe says DOWN") is True


@pytest.mark.parametrize("missing", ["token", "chat_id", "both"])
def test_send_alert_telegram_missing_credentials_returns_false(caplog, monkeypatch, missing):
    _telegram_env(monkeypatch)
    if missing in ("token", "both"):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  ")
    if missing in ("chat_id", "both"):
        monkeypatch.delenv("TELEGRAM_CHAT_ID")
    # Nothing may be posted without credentials; the autouse tripwire in
    # conftest.py also fails this test at teardown if something is.

    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        outcome = send_alert("fallback message", method="telegram")

    assert outcome is False
    # The alert is still not lost: the CRITICAL fallback line is written.
    assert any("fallback message" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502])
def test_send_alert_telegram_non_200_returns_false(caplog, monkeypatch, status):
    _telegram_env(monkeypatch)
    monkeypatch.setattr(heartbeat_mod.requests, "post", lambda url, **kw: _FakeResponse(status))

    with caplog.at_level(logging.DEBUG):
        outcome = send_alert("probe says STALE", method="telegram")

    assert outcome is False
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any(f"HTTP {status}" in r.getMessage() for r in critical)
    assert any("probe says STALE" in r.getMessage() for r in critical)
    assert FAKE_TOKEN not in _all_logged_text(caplog)


@pytest.mark.parametrize(
    "exc_type",
    [
        requests.ConnectionError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.SSLError,
        ValueError,
        OSError,
    ],
)
def test_send_alert_telegram_exception_returns_false_instead_of_raising(
    caplog, monkeypatch, exc_type
):
    _telegram_env(monkeypatch)

    def _raise(*args, **kwargs):
        raise exc_type(_leaky_text())

    monkeypatch.setattr(heartbeat_mod.requests, "post", _raise)

    with caplog.at_level(logging.DEBUG):
        outcome = send_alert("probe says DOWN", method="telegram")

    assert outcome is False
    assert FAKE_TOKEN not in _all_logged_text(caplog)
    assert any(
        exc_type.__name__ in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.CRITICAL
    )


# --------------------------------------------------------------------------- #
# delivery_method names the channel send_alert uses: "telegram" or "log_only".
# The health probe puts it in its durable ALERT_SENT / ALERT_FAILED lines and in
# the --test-alert output, so a log_only run (which send_alert reports as
# delivered: its line was written) can never be mistaken for a Telegram
# delivery. send_alert branches on the same function, so the two cannot differ.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("alert_method", "expected"),
    [
        (None, "log_only"),
        ("", "log_only"),
        ("log_only", "log_only"),
        ("telegram", "telegram"),
        ("TELEGRAM", "telegram"),
        ("Telegram", "telegram"),
        ("sms", "log_only"),  # anything but telegram takes the log_only path
    ],
    ids=["unset", "empty", "log_only", "telegram", "TELEGRAM", "Telegram", "unknown"],
)
def test_delivery_method_follows_alert_method_and_falls_back_to_log_only(
    monkeypatch, alert_method, expected
):
    if alert_method is None:
        monkeypatch.delenv("ALERT_METHOD", raising=False)
    else:
        monkeypatch.setenv("ALERT_METHOD", alert_method)

    assert heartbeat_mod.delivery_method() == expected


@pytest.mark.parametrize(
    ("explicit", "env", "expected"),
    [
        ("telegram", "log_only", "telegram"),
        ("log_only", "telegram", "log_only"),
        ("sms", "telegram", "log_only"),
        (None, "telegram", "telegram"),
    ],
    ids=["explicit-telegram", "explicit-log_only", "explicit-unknown", "no-explicit"],
)
def test_an_explicit_method_overrides_the_environment(monkeypatch, explicit, env, expected):
    monkeypatch.setenv("ALERT_METHOD", env)

    assert heartbeat_mod.delivery_method(explicit) == expected


@pytest.mark.parametrize("method", ["telegram", "log_only", "sms", None])
def test_send_alert_uses_the_channel_delivery_method_names(monkeypatch, caplog, method):
    _telegram_env(monkeypatch)  # credentials present, so only the method decides
    monkeypatch.delenv("ALERT_METHOD", raising=False)
    posts: list[str] = []

    def _post(url, **kwargs):
        posts.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(heartbeat_mod.requests, "post", _post)

    with caplog.at_level(logging.CRITICAL, logger="monitoring.heartbeat"):
        outcome = send_alert("probe says DOWN", method=method)

    assert outcome is True
    went_to_telegram = bool(posts)
    assert went_to_telegram == (heartbeat_mod.delivery_method(method) == "telegram")
    # The log_only path writes its CRITICAL line; the Telegram path does not.
    wrote_the_log_line = any("probe says DOWN" in r.getMessage() for r in caplog.records)
    assert wrote_the_log_line == (not went_to_telegram)


def test_retired_main_points_to_probe_and_exits_0(capsys):
    """python -m monitoring.heartbeat must not silently claim success -- it
    prints a pointer to the live probe and exits 0 (retired, not broken)."""
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "check_ibkr_recorder.py" in out
    assert "retired" in out.lower()
