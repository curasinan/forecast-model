"""Shared alert dispatcher for the trading pipeline (migration spec Stage 2.2)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "heartbeat.log"

log = logging.getLogger("monitoring.heartbeat")


# Logging


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


# Alerting


_REDACTED = "<redacted>"


def _scrub(text: str, secret: str) -> str:
    """``text`` with ``secret`` (and the secret half of a Telegram token) replaced."""
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
    """The channel :func:`send_alert` uses for ``method``: ``"telegram"`` or ``"log_only"``."""
    name = (method or os.environ.get("ALERT_METHOD") or METHOD_LOG_ONLY).lower()
    return METHOD_TELEGRAM if name == METHOD_TELEGRAM else METHOD_LOG_ONLY


def send_alert(message: str, method: str | None = None) -> bool:
    """Dispatch an alert."""
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
            failure = f"telegram send error: {type(exc).__name__}"
        if failure is None:
            log.info("alert sent via telegram")
            return True
        log.critical("%s", _scrub(f"ALERT ({failure}): {safe_message}", token))
        return False

    # log_only (default)
    log.critical("ALERT: %s", message)
    return True


# CLI (retired)


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
