"""Shared alert dispatcher for the trading pipeline (migration spec Stage 2.2).

This module used to run three Polymarket-era health checks of its own
(snapshot freshness, snapshot growth, collector-log success-in-window)
against ``data/polymarket.db``. That collection pipeline is retired (see
``docs/MIGRATION_CHECKPOINT.md``): the checks and the module constants that
only they used (``DEFAULT_DB_PATH``, ``DEFAULT_STATE_PATH``,
``DEFAULT_LOG_DIR``, ``RISK_CONFIG_PATH``, the success-window constants) were
deleted 2026-08-27 rather than left as dead code silently reporting on a
pipeline that no longer runs.

What remains, and why:

- ``send_alert`` -- the live alert dispatcher (Telegram via the
  ``ALERT_METHOD`` / ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` env vars,
  else a ``log_only`` CRITICAL log line; Telegram failures degrade to
  ``log_only`` rather than losing the alert). It returns whether the alert
  was delivered, so the probe can retry one that was not; ``delivery_method``
  names the channel it uses (``telegram`` or ``log_only``), so the probe can
  say which one an outcome belongs to. ``send_alert`` is imported directly by
  ``scripts/check_ibkr_recorder.py`` -- the current IBKR recorder/paper-sync
  health probe -- and must keep this import path
  (``from monitoring.heartbeat import send_alert``) stable.
- ``setup_logging`` -- generic file+console logging setup for this logger.
  Nothing in this module's own retired CLI calls it any more (the pointer
  below is a bare ``print``, not a logged message), and the probe needs
  nothing from it either. It is kept because it is not Polymarket-specific
  and costs nothing to leave in place for any future caller that wants
  ``monitoring.heartbeat``'s logger configured with handlers.

Retired entrypoint:
    python -m monitoring.heartbeat

now just prints a pointer to the current probe and exits 0 -- loud about
being retired rather than silently claiming to have checked anything.
Current health checks: ``python scripts/check_ibkr_recorder.py``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "heartbeat.log"

log = logging.getLogger("monitoring.heartbeat")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #


_REDACTED = "<redacted>"


def _scrub(text: str, secret: str) -> str:
    """``text`` with every occurrence of ``secret`` replaced -- and of the
    secret half of a ``<bot-id>:<secret>`` Telegram token too, so a partial
    echo cannot leak the part that matters. Belt only: the structure of
    :func:`send_alert` already keeps the token away from every log call."""
    if not secret:
        return text
    text = text.replace(secret, _REDACTED)
    _, _, tail = secret.partition(":")
    if tail:
        text = text.replace(tail, _REDACTED)
    return text


METHOD_TELEGRAM = "telegram"
METHOD_LOG_ONLY = "log_only"


def delivery_method(method: str | None = None) -> str:
    """The channel :func:`send_alert` uses for ``method``: ``"telegram"`` or
    ``"log_only"``.

    ``method`` defaults to the ALERT_METHOD env var, then ``log_only``; the
    match is case-insensitive and anything that is not "telegram" takes the
    log_only path. This is the one place that resolves the method:
    :func:`send_alert` branches on it, and the health probe names it in its
    durable ALERT_SENT / ALERT_FAILED lines and in the ``--test-alert`` output.
    ``send_alert`` reports a log_only run as delivered (its line was written),
    so without the method a log_only run could pass for a Telegram delivery.
    """
    name = (method or os.environ.get("ALERT_METHOD") or METHOD_LOG_ONLY).lower()
    return METHOD_TELEGRAM if name == METHOD_TELEGRAM else METHOD_LOG_ONLY


def send_alert(message: str, method: str | None = None) -> bool:
    """Dispatch an alert. ``method`` defaults to the ALERT_METHOD env var, then
    ``log_only`` (see :func:`delivery_method`). Telegram failures fall back to
    a CRITICAL log line so an alert is never silently lost.

    Returns the outcome: ``True`` when the alert was delivered (Telegram
    answered HTTP 200, or the ``log_only`` method wrote its line), ``False``
    when it was not (``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` missing, a
    non-200 answer, or any exception). A caller that does not care can ignore
    the result; the health probe uses it to retry an undelivered alert on its
    next run instead of recording it as sent.

    SECRET HYGIENE (final-review finding C-2): Telegram's API puts the bot
    token in the URL PATH (``/bot<TOKEN>/sendMessage``), and requests'
    connection-error text quotes that path verbatim ("Max retries exceeded
    with url: /bot<TOKEN>/sendMessage"). The health probe's stderr is
    appended to ``logs/ibkr_health_task.log`` (a durable, OneDrive-synced
    file) -- hourly during exactly the outages that make this path fire. So
    the exception object is NEVER formatted into a log line here: only its
    class name is, and every line on the Telegram path is additionally
    passed through :func:`_scrub`. Any exception at all is contained (not
    just ``RequestException``): one escaping would reach the probe's own
    ``print(... exc ...)`` fallback on the same stderr channel.
    """
    if delivery_method(method) == METHOD_TELEGRAM:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.critical(
                "ALERT (telegram requested but TELEGRAM_BOT_TOKEN/CHAT_ID unset): %s",
                message,
            )
            return False
        safe_message = _scrub(message, token)
        failure: str | None = None
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=15,
            )
            if resp.status_code != 200:
                failure = f"telegram send failed, HTTP {int(resp.status_code)}"
        except Exception as exc:  # noqa: BLE001 - contain ANY failure; see docstring
            # type(exc).__name__ only -- never str(exc)/repr(exc)/exc_info:
            # the exception text embeds the request URL, token included.
            failure = f"telegram send error: {type(exc).__name__}"
        if failure is None:
            log.info("alert sent via telegram")
            return True
        # Formatted OUTSIDE the except block, from a string that never saw
        # the exception text; scrubbed once more as the belt.
        log.critical("%s", _scrub(f"ALERT ({failure}): {safe_message}", token))
        return False

    # log_only (default)
    log.critical("ALERT: %s", message)
    return True


# --------------------------------------------------------------------------- #
# CLI (retired)
# --------------------------------------------------------------------------- #


def main() -> int:
    print(
        "monitoring.heartbeat is retired as a runnable health check -- the "
        "Polymarket collection pipeline it used to check is retired (see "
        "docs/MIGRATION_CHECKPOINT.md). It still provides send_alert() for "
        "other tools. Current health probe: python scripts/check_ibkr_recorder.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
