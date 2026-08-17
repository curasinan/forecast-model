"""Health probe for the IBKR quote recorder (migration spec Stage 2.2).

Read-only and cron / Task-Scheduler friendly: it inspects the recorder's
heartbeat state file, the connection-events JSONL, and (when healthy)
today's parquet partition. It never touches the Gateway and never imports
ib_async, so it is safe to run at any time and finishes in well under 5
seconds.

Statuses (the message is a single ASCII line; every non-OK status is also
dispatched through monitoring.heartbeat.send_alert, which honors the
ALERT_METHOD / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars):

    OK        exit 0  heartbeat fresh; reports age + today's parquet count
    STALE     exit 1  heartbeat older than --max-age-minutes
    CRITICAL  exit 1  state file missing or unreadable
    DOWN      exit 1  last event is an unexpected disconnect, no reconnect yet

Motivating incident (2026-08-16): Gateway's nightly auto-logoff silently
stalled collection for 15 hours while the recorder retried every 60s. A
scheduled run of this probe turns that silence into an alert.

Usage:
    python scripts/check_ibkr_recorder.py
    python scripts/check_ibkr_recorder.py --max-age-minutes 90 --quiet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.heartbeat import send_alert  # noqa: E402
from src.utils.dates import parse_iso8601  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS_PATH = REPO_ROOT / "logs" / "ibkr_events.jsonl"
# 3x the 20-minute sweep cadence: one sweep takes ~13 min, so a healthy
# recorder can never legitimately be more than ~2 intervals behind.
DEFAULT_MAX_AGE_MINUTES = 60.0
EVENTS_TAIL_LINES = 5

# "gave_up_window" is the gateway's "still down after the expected nightly
# restart window closed" signal -- operationally a DOWN, not an expected blip.
_DISCONNECT_EVENTS = {
    "disconnect", "disconnected", "unexpected_disconnect", "connection_lost",
    "gave_up_window",
}
_RECONNECT_EVENTS = {"reconnect", "reconnected", "connect", "connected"}


def default_state_path() -> Path:
    """Heartbeat path under the recorder's data root.

    Mirrors IBKRSettings.resolved_data_root's fallback convention
    (%LOCALAPPDATA%/ibkr_ticks, then ~/.local/share off Windows) WITHOUT
    constructing IBKRSettings: the settings class fails loudly when
    IBKR_PORT / IBKR_TRADING_MODE are unset (by design, spec Stage 7),
    and a monitoring probe must keep working in a bare scheduler env.
    """
    local_app = os.environ.get("LOCALAPPDATA")
    root = Path(local_app) / "ibkr_ticks" if local_app else (
        Path.home() / ".local" / "share" / "ibkr_ticks")
    return root / "state" / "recorder_state.json"


# --------------------------------------------------------------------------- #
# Probe pieces
# --------------------------------------------------------------------------- #


def _read_state(state_path: Path) -> tuple[dict | None, str | None]:
    """Return (state_dict, error). Exactly one of the two is None."""
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "state JSON is not an object"
    return data, None


def _last_event(events_path: Path) -> dict | None:
    """Parse the last non-blank line of the events JSONL, or None.

    Missing file, blank tail, or an unparseable last line all yield None:
    the events file is a secondary signal and its absence is not a failure.
    """
    if not events_path.exists():
        return None
    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=EVENTS_TAIL_LINES)
    except OSError:
        return None
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None
    return None


def _is_unexpected_disconnect(event: dict) -> bool:
    """True when the event records a disconnect that was not a planned stop.

    Accepts both the gateway's ConnectionEvent schema ("kind"/"ts_utc",
    src/venues/ibkr/gateway.py) and the generic "event"/"type" + "ts" shape.
    """
    name = str(event.get("kind") or event.get("event") or event.get("type") or "").lower()
    return name in _DISCONNECT_EVENTS and not event.get("expected", False)


def _count_today_parquet(state_path: Path, now: datetime) -> int | None:
    """Count today's sweep files under the quotes dir sibling of state/.

    Layout (recorder.py): <data_root>/state/recorder_state.json and
    <data_root>/quotes/date=YYYY-MM-DD/*.parquet, UTC-partitioned. Returns
    None when the partition is absent or unreadable (count is best-effort
    flavor for the OK message, never a failure condition).
    """
    quotes_dir = state_path.parent.parent / "quotes" / f"date={now:%Y-%m-%d}"
    try:
        if not quotes_dir.is_dir():
            return None
        return sum(1 for _ in quotes_dir.glob("*.parquet"))
    except OSError:
        return None


def run_check(
    state_path: Path | str,
    events_path: Path | str,
    max_age_minutes: float,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Run the probe. Returns (status, one-line ASCII message).

    status is one of "OK" / "STALE" / "CRITICAL" / "DOWN"; only "OK" is
    healthy. Pure function of the filesystem -- no network, no side effects.
    """
    now = now or datetime.now(timezone.utc)
    state_path = Path(state_path)

    if not state_path.exists():
        return "CRITICAL", (
            f"ibkr recorder CRITICAL: state file missing at {state_path} "
            "(recorder never ran, or wrong data root)"
        )

    state, err = _read_state(state_path)
    if state is None:
        return "CRITICAL", f"ibkr recorder CRITICAL: state file unreadable at {state_path} ({err})"
    heartbeat_ts = parse_iso8601(state.get("ts"))
    if heartbeat_ts is None:
        return "CRITICAL", (
            f"ibkr recorder CRITICAL: state file at {state_path} has no parseable 'ts'"
        )

    # DOWN outranks STALE: an explicit unexpected-disconnect event with no
    # reconnect after it is a more specific diagnosis than a stale heartbeat
    # (the auto-logoff incident produces exactly this signature).
    event = _last_event(Path(events_path))
    if event is not None and _is_unexpected_disconnect(event):
        event_ts = parse_iso8601(event.get("ts") or event.get("ts_utc"))
        if event_ts is not None:
            down_min = (now - event_ts).total_seconds() / 60.0
            downtime = f"down for {down_min:.0f} min so far"
        else:
            downtime = "downtime unknown (event has no parseable 'ts')"
        return "DOWN", (
            f"ibkr recorder DOWN: unexpected disconnect with no reconnect since ({downtime})"
        )

    age_min = (now - heartbeat_ts).total_seconds() / 60.0
    rows = state.get("rows")
    sweep_seconds = state.get("sweep_seconds")
    if age_min > max_age_minutes:
        return "STALE", (
            f"ibkr recorder STALE: heartbeat is {age_min:.0f} min old "
            f"(threshold {max_age_minutes:.0f} min); last sweep rows={rows} "
            f"sweep_seconds={sweep_seconds}"
        )

    n_files = _count_today_parquet(state_path, now)
    files_part = "n/a" if n_files is None else str(n_files)
    return "OK", (
        f"ibkr recorder OK: heartbeat {age_min:.1f} min old; last sweep rows={rows} "
        f"sweep_seconds={sweep_seconds}; parquet files today={files_part}"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-path", default=str(default_state_path()),
                    help="recorder heartbeat JSON (default: %(default)s)")
    ap.add_argument("--events-path", default=str(DEFAULT_EVENTS_PATH),
                    help="recorder connection-events JSONL (default: %(default)s)")
    ap.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES,
                    help="heartbeat age beyond which the recorder is STALE "
                         "(default: %(default)s = 3x the 20-min sweep interval)")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout (alerts still fire)")
    args = ap.parse_args(argv)

    status, message = run_check(args.state_path, args.events_path, args.max_age_minutes)
    if not args.quiet:
        print(message)
    if status == "OK":
        return 0
    # send_alert already degrades Telegram failures to a CRITICAL log line,
    # but nothing it could ever raise may mask the unhealthy exit code.
    try:
        send_alert(message)
    except Exception as exc:  # noqa: BLE001 - exit code must survive any alert failure
        if not args.quiet:
            print(f"send_alert failed ({exc}); alert may be lost", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
