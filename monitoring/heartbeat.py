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
  ``log_only`` rather than losing the alert). This is imported directly by
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


def send_alert(message: str, method: str | None = None) -> None:
    """Dispatch an alert. ``method`` defaults to the ALERT_METHOD env var, then
    ``log_only``. Telegram failures fall back to a CRITICAL log line so an
    alert is never silently lost."""
    method = (method or os.environ.get("ALERT_METHOD") or "log_only").lower()

    if method == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.critical(
                "ALERT (telegram requested but TELEGRAM_BOT_TOKEN/CHAT_ID unset): %s",
                message,
            )
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=15,
            )
            if resp.status_code == 200:
                log.info("alert sent via telegram")
            else:
                # Never log the token; just the status.
                log.critical(
                    "ALERT (telegram send failed, HTTP %s): %s",
                    resp.status_code, message,
                )
        except requests.RequestException as exc:
            log.critical("ALERT (telegram send error: %s): %s", exc, message)
        return

    # log_only (default)
    log.critical("ALERT: %s", message)


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
