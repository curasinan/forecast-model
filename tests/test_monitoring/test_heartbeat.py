"""Tests for monitoring.heartbeat: the shared alert dispatcher."""

from __future__ import annotations

import logging

import pytest
import requests

import monitoring.heartbeat as heartbeat_mod
from monitoring.heartbeat import main, send_alert

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


def _leaky_text() -> str:
    return (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: /bot{FAKE_TOKEN}/sendMessage (Caused by NewConnectionError("
        "'<urllib3.connection.HTTPSConnection object>: Failed to establish a new "
        "connection: [Errno 11001] getaddrinfo failed'))"
    )


def _all_logged_text(caplog) -> str:
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
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "the CRITICAL fallback must still fire"
    assert any("probe says DOWN" in r.getMessage() for r in critical)
    assert any(exc_type.__name__ in r.getMessage() for r in critical)


def test_telegram_unexpected_exception_is_also_contained_without_the_token(
    caplog, capsys, monkeypatch
):
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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)

    def _raise(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(heartbeat_mod.requests, "post", _raise)

    with caplog.at_level(logging.DEBUG):
        send_alert(f"weird alert mentioning {FAKE_TOKEN}", method="telegram")

    assert FAKE_TOKEN not in _all_logged_text(caplog)
    assert any("weird alert mentioning" in r.getMessage() for r in caplog.records)


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
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert FAKE_TOKEN not in _all_logged_text(caplog)


def test_send_alert_env_selected_telegram_returns_true(monkeypatch):
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
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "check_ibkr_recorder.py" in out
    assert "retired" in out.lower()
