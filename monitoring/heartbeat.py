"""System health heartbeat for the trading pipeline.

Runs three checks and alerts (Telegram or log-only) on any failure:

1. Snapshot freshness — newest snapshot must be within ``max_snapshot_age_minutes``.
2. Growth — the snapshots table must have grown since the previous heartbeat.
3. Collector success-in-window — recent successful run lines in the collector logs.

Runnable for the scheduler (every ~30 min):
    python -m monitoring.heartbeat

Alerting via the ``ALERT_METHOD`` env var:
    log_only  (default) — write a CRITICAL log line
    telegram            — POST to the Telegram Bot API using
                          TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars

Exit code 0 when healthy, 1 when any check fails (so the scheduler's
LastTaskResult reflects health).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.utils.dates import parse_iso8601

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"
DEFAULT_STATE_PATH = REPO_ROOT / "monitoring" / "heartbeat_state.json"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
RISK_CONFIG_PATH = REPO_ROOT / "config" / "risk_limits.json"
LOG_PATH = REPO_ROOT / "logs" / "heartbeat.log"

DEFAULT_MAX_AGE_MINUTES = 45.0
# Each collector's expected "I ran successfully recently" window.
MARKETS_SUCCESS_WINDOW_HOURS = 7.0      # scheduled every 6h
SNAPSHOTS_SUCCESS_WINDOW_MINUTES = 40.0  # scheduled every 15min
LOG_TAIL_LINES = 50

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
# Config
# --------------------------------------------------------------------------- #


def _load_max_age_minutes(risk_config_path: Path | str = RISK_CONFIG_PATH) -> float:
    """Read max_snapshot_age_minutes from the risk config (kept in sync with
    the signal generator's freshness guard). Falls back to the default."""
    p = Path(risk_config_path)
    if not p.exists():
        return DEFAULT_MAX_AGE_MINUTES
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        return float(cfg.get("max_snapshot_age_minutes", DEFAULT_MAX_AGE_MINUTES))
    except (json.JSONDecodeError, TypeError, ValueError):
        return DEFAULT_MAX_AGE_MINUTES


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #


def run_heartbeat(
    db_path: Path | str = DEFAULT_DB_PATH,
    state_path: Path | str = DEFAULT_STATE_PATH,
    log_dir: Path | str = DEFAULT_LOG_DIR,
    max_age_minutes: float | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Run all health checks. Returns a list of failure messages (empty == healthy).

    State (last snapshot count + check time) is read from and written back to
    ``state_path`` so the growth check has a previous reference.
    """
    if max_age_minutes is None:
        max_age_minutes = _load_max_age_minutes()
    now = now or datetime.now(timezone.utc)
    failures: list[str] = []

    # --- Check 1 + 2: snapshot freshness + growth (single DB open) -------- #
    current_count: int | None = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            newest_str = conn.execute("SELECT MAX(timestamp) FROM snapshots").fetchone()[0]
            current_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        failures.append(f"DB error reading snapshots: {exc}")
        newest_str = None

    # Check 1: freshness
    newest = parse_iso8601(newest_str) if newest_str else None
    if newest is None:
        failures.append("no snapshots in DB (table empty or unreadable)")
    else:
        age_min = (now - newest).total_seconds() / 60.0
        if age_min > max_age_minutes:
            failures.append(
                f"snapshot data is stale: newest is {age_min:.0f} min old "
                f"(threshold {max_age_minutes:.0f} min) — collection pipeline likely down"
            )

    # Check 2: growth since last heartbeat.
    # Only flag after TWO consecutive no-growth observations — a single stall
    # is expected when the 30-min heartbeat lands between 15-min snapshot runs.
    prev_state = _read_state(state_path)
    consecutive_no_growth = 0
    if current_count is not None:
        prev_count = prev_state.get("last_count")
        prev_streak = int(prev_state.get("consecutive_no_growth", 0) or 0)
        if prev_count is not None and current_count <= int(prev_count):
            consecutive_no_growth = prev_streak + 1
            if consecutive_no_growth >= 2:
                failures.append(
                    f"snapshots table did not grow across {consecutive_no_growth} "
                    f"consecutive heartbeats (stuck at {current_count}) — "
                    f"collection pipeline likely down"
                )
        _write_state(state_path, {
            "last_count": int(current_count),
            "last_checked": now.isoformat(),
            "consecutive_no_growth": consecutive_no_growth,
        })

    # --- Check 3: collector success-in-window ---------------------------- #
    failures.extend(_check_collector_log(
        Path(log_dir) / "market_collector.log",
        success_markers=("Collection run finished with exit code 0", "Run summary:"),
        window=timedelta(hours=MARKETS_SUCCESS_WINDOW_HOURS),
        label="collect_markets",
        now=now,
    ))
    failures.extend(_check_collector_log(
        Path(log_dir) / "snapshot_collector.log",
        success_markers=("Run summary:",),
        window=timedelta(minutes=SNAPSHOTS_SUCCESS_WINDOW_MINUTES),
        label="collect_snapshots",
        now=now,
    ))

    return failures


def _check_collector_log(
    log_path: Path,
    success_markers: tuple[str, ...],
    window: timedelta,
    label: str,
    now: datetime,
) -> list[str]:
    """Confirm a success marker appears in the log tail within ``window``."""
    if not log_path.exists():
        return [f"{label}: log file not found at {log_path}"]
    try:
        lines = _tail(log_path, LOG_TAIL_LINES)
    except OSError as exc:
        return [f"{label}: cannot read log ({exc})"]

    cutoff = now - window
    for line in reversed(lines):
        if any(marker in line for marker in success_markers):
            ts = _parse_log_timestamp(line)
            if ts is not None and ts >= cutoff:
                return []  # found a recent success
    return [
        f"{label}: no successful run found in the last "
        f"{window.total_seconds() / 3600:.1f}h (log tail had no recent success marker)"
    ]


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
# State + log helpers
# --------------------------------------------------------------------------- #


def _read_state(state_path: Path | str) -> dict:
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state_path: Path | str, state: dict) -> None:
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _tail(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines without loading the whole file."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=n))


def _parse_log_timestamp(line: str) -> datetime | None:
    """Collector logs start with an ISO 8601 timestamp followed by ' ['."""
    head = line.split(" [", 1)[0].strip()
    return parse_iso8601(head) if head else None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    setup_logging()
    log.info("Running heartbeat health check")
    failures = run_heartbeat()
    if not failures:
        log.info("heartbeat: all checks PASS")
        return 0

    for f in failures:
        log.error("heartbeat FAILURE: %s", f)
    send_alert(
        "Trading pipeline heartbeat detected problems:\n- " + "\n- ".join(failures)
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
