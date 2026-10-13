"""Health probe for the IBKR quote recorder + paper-book sync (migration"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __name__ == "__main__":
    def _crash_exit(exc_type, exc, tb):
        import traceback
        traceback.print_exception(exc_type, exc, tb)
        os._exit(3)
    sys.excepthook = _crash_exit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.heartbeat import METHOD_TELEGRAM, delivery_method, send_alert  # noqa: E402
from src.utils.dates import parse_iso8601  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS_PATH = REPO_ROOT / "logs" / "ibkr_events.jsonl"
DEFAULT_HEALTH_LOG_PATH = REPO_ROOT / "logs" / "ibkr_health.log"
DEFAULT_ALERT_STATE_PATH = REPO_ROOT / "logs" / "ibkr_health_alert_state.json"
DEFAULT_SNAPSHOT_PATH = REPO_ROOT / "data" / "ibkr_universe_live.json"
DEFAULT_MAX_AGE_MINUTES = 60.0
# One daily sync cadence (17:30 -> 17:30) + 2h slack for a late run.
DEFAULT_MAX_SYNC_AGE_HOURS = 26.0
DEGRADED_ROWS_FRACTION = 0.5
EVENTS_TAIL_LINES = 5
SYNC_RUNS_FILENAME = "ibkr_sync_runs.jsonl"

_DISCONNECT_EVENTS = {
    "disconnect", "disconnected", "unexpected_disconnect", "connection_lost",
    "gave_up_window",
}
_RECONNECT_EVENTS = {"reconnect", "reconnected", "connect", "connected"}


def default_state_path() -> Path:
    """Heartbeat path under the recorder's data root."""
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path.home() / "quant_data" / "ibkr_ticks"
    return root / "state" / "recorder_state.json"


def default_sync_runs_path() -> Path:
    """Paper-sync run ledger under the same data root as ``default_state_path``."""
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path.home() / "quant_data" / "ibkr_ticks"
    return root / "paper" / SYNC_RUNS_FILENAME


# Probe pieces


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
    """Parse the last non-blank line of the events JSONL, or None."""
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
    """True when the event records a disconnect that was not a planned stop."""
    name = str(event.get("kind") or event.get("event") or event.get("type") or "").lower()
    return name in _DISCONNECT_EVENTS and not event.get("expected", False)


def _count_today_parquet(state_path: Path, now: datetime) -> int | None:
    """Count today's sweep files under the quotes dir sibling of state/."""
    quotes_dir = state_path.parent.parent / "quotes" / f"date={now:%Y-%m-%d}"
    try:
        if not quotes_dir.is_dir():
            return None
        return sum(1 for _ in quotes_dir.glob("*.parquet"))
    except OSError:
        return None


def _snapshot_age_days(snapshot_path: Path, now: datetime) -> int | None:
    """Whole days since the universe snapshot's mtime, or None if missing/unreadable."""
    try:
        mtime = snapshot_path.stat().st_mtime
    except OSError:
        return None
    return int((now.timestamp() - mtime) / 86400.0)


def run_check(
    state_path: Path | str,
    events_path: Path | str,
    max_age_minutes: float,
    now: datetime | None = None,
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
) -> tuple[str, str]:
    """Run the recorder-heartbeat axis. Returns (status, one-line ASCII message)."""
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

    universe_size = state.get("universe_size")
    if isinstance(universe_size, (int, float)) and universe_size > 0 and rows is not None:
        try:
            rows_f = float(rows)
        except (TypeError, ValueError):
            rows_f = None
        if rows_f is not None and rows_f < DEGRADED_ROWS_FRACTION * universe_size:
            return "DEGRADED", (
                f"ibkr recorder DEGRADED: rows={rows} below "
                f"{DEGRADED_ROWS_FRACTION:.0%} of universe_size={universe_size} "
                f"(heartbeat fresh at {age_min:.1f} min old; sweep_seconds={sweep_seconds})"
            )

    n_files = _count_today_parquet(state_path, now)
    files_part = "n/a" if n_files is None else str(n_files)
    age_days = _snapshot_age_days(Path(snapshot_path), now)
    snapshot_part = "n/a" if age_days is None else str(age_days)
    return "OK", (
        f"ibkr recorder OK: heartbeat {age_min:.1f} min old; last sweep rows={rows} "
        f"sweep_seconds={sweep_seconds}; parquet files today={files_part}; "
        f"snapshot_age_days={snapshot_part}"
    )


def _last_ok_sync_ts(sync_runs_path: Path) -> tuple[datetime | None, bool]:
    """Return (timestamp of the LAST "ok": true line, file_exists)."""
    if not sync_runs_path.exists():
        return None, False
    last_ok_ts: datetime | None = None
    try:
        with sync_runs_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("ok") is not True:
                    continue
                ts = parse_iso8601(record.get("ts_utc"))
                if ts is not None:
                    last_ok_ts = ts
    except OSError:
        return None, True
    return last_ok_ts, True


def _check_sync_freshness(
    sync_runs_path: Path | str,
    max_sync_age_hours: float,
    now: datetime,
) -> tuple[str, str]:
    """Run the paper-sync axis. Returns ("OK", msg) or ("SYNC_STALE", msg)."""
    sync_runs_path = Path(sync_runs_path)
    last_ok_ts, exists = _last_ok_sync_ts(sync_runs_path)

    if not exists:
        return "SYNC_STALE", (
            f"ibkr paper sync SYNC_STALE: run ledger missing at {sync_runs_path} "
            "(sync never ran, or wrong data root)"
        )
    if last_ok_ts is None:
        return "SYNC_STALE", (
            f"ibkr paper sync SYNC_STALE: no successful run recorded in {sync_runs_path}"
        )

    age_hours = (now - last_ok_ts).total_seconds() / 3600.0
    if age_hours > max_sync_age_hours:
        return "SYNC_STALE", (
            f"ibkr paper sync SYNC_STALE: last successful sync is {age_hours:.1f}h old "
            f"(threshold {max_sync_age_hours:.1f}h)"
        )
    return "OK", f"ibkr paper sync OK: last successful sync {age_hours:.1f}h ago"


def _combine_axes(
    recorder_status: str,
    recorder_message: str,
    sync_status: str,
    sync_message: str,
) -> tuple[str, str]:
    """Combine the two axes into one (status, message)."""
    recorder_failed = recorder_status != "OK"
    sync_failed = sync_status != "OK"
    if recorder_failed and sync_failed:
        return recorder_status, f"{recorder_message} | {sync_message}"
    if recorder_failed:
        return recorder_status, recorder_message
    if sync_failed:
        return sync_status, sync_message
    return "OK", recorder_message


def _append_health_log(
    log_path: Path | str,
    status: str,
    message: str,
    now: datetime | None = None,
) -> None:
    """Durable sink (H3): append one line for a non-OK result."""
    now = now or datetime.now(timezone.utc)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{now.isoformat()} {status} {message}\n")


@dataclass(frozen=True)
class AlertOutcome:
    """How one send attempt went."""

    delivered: bool
    reason: str | None
    method: str


def _append_alert_log(
    log_path: Path | str,
    kind: str,
    status: str,
    outcome: AlertOutcome,
    now: datetime,
) -> None:
    """Durable record of one send attempt, beside the per-run status lines."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    word = "ALERT_SENT" if outcome.delivered else "ALERT_FAILED"
    line = f"{now.isoformat()} {word} {kind} {status} {outcome.method}"
    if not outcome.delivered:
        line += f" {outcome.reason}"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# Alert state: alert on state changes, not on every unhealthy run

OK_PAIR = ("OK", "OK")
ALERT_REMINDER_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class AlertState:
    """What the previous runs told the operator: the persisted alert state."""

    recorder_status: str = "OK"
    sync_status: str = "OK"
    since: datetime | None = None
    last_alert: datetime | None = None
    last_alert_ok: bool | None = None
    outage_since: datetime | None = None
    recovered_from: tuple[str, str] | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.recorder_status, self.sync_status)


NO_ALERT_STATE = AlertState()


@dataclass(frozen=True)
class AlertDecision:
    """``kind`` is "transition", "reminder" or "retry" (None: stay silent)."""

    kind: str | None
    prefix: str
    state: AlertState


def _pair_label(pair: tuple[str, str]) -> str:
    """Every failing axis, joined: "OK", "DOWN", "SYNC_STALE", "DOWN+SYNC_STALE"."""
    failing = [status for status in pair if status != "OK"]
    return "+".join(failing) if failing else "OK"


def _format_duration(delta: timedelta) -> str:
    """Two units, floored: "4d 12h", "3h 20m", "7m". Negative reads as "0m"."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _recovery_prefix(before: tuple[str, str], elapsed: str) -> str:
    """The recovery message's prefix: "[DOWN -> OK] recovered after 4d 12h:"."""
    return f"[{_pair_label(before)} -> OK] recovered after {elapsed}:"


def decide_alert(previous: AlertState, pair: tuple[str, str], now: datetime) -> AlertDecision:
    """Whether this run should alert, and what the successor state looks like."""
    if pair != previous.pair:
        kind = "transition"
    elif previous.last_alert_ok is False:
        kind = "retry"
    elif pair == OK_PAIR:
        return AlertDecision(None, "", previous)
    elif previous.last_alert_ok is None or previous.last_alert is None:
        kind = "retry"
    elif previous.last_alert > now or now - previous.last_alert >= ALERT_REMINDER_INTERVAL:
        kind = "reminder"
    else:
        return AlertDecision(None, "", previous)

    if kind == "retry" and pair == OK_PAIR:
        recovered_at = previous.since or now
        elapsed = _format_duration(recovered_at - (previous.outage_since or recovered_at))
        before = previous.recovered_from or ("UNKNOWN", "OK")
        return AlertDecision(kind, _recovery_prefix(before, elapsed), previous)

    outage_start = (previous.outage_since if previous.pair != OK_PAIR else None) or now
    elapsed = _format_duration(now - outage_start)
    if kind != "transition":
        return AlertDecision(kind, f"[still {_pair_label(pair)}, {elapsed}]", previous)

    if pair == OK_PAIR:
        successor = AlertState(
            pair[0], pair[1], since=now, outage_since=outage_start, recovered_from=previous.pair
        )
        return AlertDecision(kind, _recovery_prefix(previous.pair, elapsed), successor)
    successor = AlertState(pair[0], pair[1], since=now, outage_since=outage_start)
    prefix = f"[{_pair_label(previous.pair)} -> {_pair_label(pair)}]"
    return AlertDecision(kind, prefix, successor)


def record_alert_attempt(state: AlertState, now: datetime, delivered: bool) -> AlertState:
    """``state`` with the send attempt just made stamped on it."""
    stamped = replace(state, last_alert=now, last_alert_ok=delivered)
    if delivered and state.pair == OK_PAIR:
        return replace(stamped, outage_since=None, recovered_from=None)
    return stamped


_STATUS_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,31}")


def _pair_from_json(value: object) -> tuple[str, str] | None:
    """A ``[recorder_status, sync_status]`` list, or None for anything else."""
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(s, str) and _STATUS_PATTERN.fullmatch(s) for s in value)
    ):
        return (value[0], value[1])
    return None


def _alert_state_from_json(raw: object) -> AlertState | None:
    """The state a decoded JSON document describes, or None when it is not well-formed."""
    if not isinstance(raw, dict):
        return None
    recorder_status, sync_status = raw.get("recorder_status"), raw.get("sync_status")
    for status in (recorder_status, sync_status):
        if not isinstance(status, str) or not _STATUS_PATTERN.fullmatch(status):
            return None
    last_alert_ok = raw.get("last_alert_ok")
    if last_alert_ok is not None and not isinstance(last_alert_ok, bool):
        return None
    stamps: dict[str, datetime | None] = {}
    for key in ("since_utc", "last_alert_utc", "outage_since_utc"):
        value = raw.get(key)
        stamp = parse_iso8601(value) if isinstance(value, str) else None
        if value is not None and stamp is None:
            return None
        stamps[key] = stamp
    state = AlertState(
        recorder_status,
        sync_status,
        since=stamps["since_utc"],
        last_alert=stamps["last_alert_utc"],
        last_alert_ok=last_alert_ok,
        outage_since=stamps["outage_since_utc"],
        recovered_from=_pair_from_json(raw.get("recovered_from")),
    )
    if state.pair != OK_PAIR and state.outage_since is None:
        return None
    return state


def load_alert_state(path: Path | str) -> AlertState:
    """The persisted alert state, or ``NO_ALERT_STATE``. Never raises."""
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            raw = json.load(f)
        state = _alert_state_from_json(raw)
    except (OSError, ValueError, RecursionError, OverflowError):
        return NO_ALERT_STATE
    return state or NO_ALERT_STATE


def _iso(stamp: datetime | None) -> str | None:
    return None if stamp is None else stamp.isoformat()


def save_alert_state(path: Path | str, state: AlertState) -> None:
    """Persist ``state`` atomically: a temp file in the same directory, then ``os.replace``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorder_status": state.recorder_status,
        "sync_status": state.sync_status,
        "since_utc": _iso(state.since),
        "last_alert_utc": _iso(state.last_alert),
        "last_alert_ok": state.last_alert_ok,
        "outage_since_utc": _iso(state.outage_since),
        "recovered_from": None if state.recovered_from is None else list(state.recovered_from),
    }
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


_NOT_DELIVERED = "not_delivered"


def _deliver_alert(text: str, *, quiet: bool) -> AlertOutcome:
    """Send ``text`` through send_alert and say how it went (see AlertOutcome)."""
    method = delivery_method()
    try:
        delivered = send_alert(text) is True
    except Exception as exc:  # noqa: BLE001 - the exit code must survive any alert failure
        if not quiet:
            print(f"send_alert failed ({type(exc).__name__}); alert may be lost",
                  file=sys.stderr)
        return AlertOutcome(False, type(exc).__name__, method)
    if delivered:
        return AlertOutcome(True, None, method)
    return AlertOutcome(False, _NOT_DELIVERED, method)


def _save_attempt(
    args: argparse.Namespace,
    state_path: Path,
    state: AlertState,
    now: datetime,
    delivered: bool,
) -> None:
    """Persist ``state`` stamped with an attempt that was (not) delivered."""
    try:
        save_alert_state(state_path, record_alert_attempt(state, now, delivered))
    except Exception as exc:  # noqa: BLE001 - exit code must survive a state-file failure
        if not args.quiet:
            print(f"alert state write failed ({type(exc).__name__}); "
                  "alerts may repeat or be missed until the state file can be written again",
                  file=sys.stderr)


def _alert_on_change(
    args: argparse.Namespace,
    pair: tuple[str, str],
    status: str,
    message: str,
    now: datetime,
) -> None:
    """Alert on a changed (recorder, sync) pair, remind during an outage, retry lost sends."""
    state_path = Path(args.alert_state_path)
    try:
        previous = load_alert_state(state_path)
    except Exception:  # noqa: BLE001 - it should not raise; if it does, still alert
        previous = NO_ALERT_STATE
    decision = decide_alert(previous, pair, now)
    if decision.kind is None:
        return

    _save_attempt(args, state_path, decision.state, now, delivered=False)
    outcome = _deliver_alert(f"{decision.prefix} {message}", quiet=args.quiet)
    try:
        _append_alert_log(args.health_log_path, decision.kind, status, outcome, now)
    except Exception as exc:  # noqa: BLE001 - the state update below must still run
        if not args.quiet:
            print(f"alert log write failed ({type(exc).__name__}); "
                  "the send outcome may be unrecorded", file=sys.stderr)
    _save_attempt(args, state_path, decision.state, now, delivered=outcome.delivered)


def _describe_test_alert(outcome: AlertOutcome) -> str:
    """The line ``--test-alert`` prints: the outcome and the method it used."""
    if not outcome.delivered:
        return f"test alert NOT delivered via {outcome.method} ({outcome.reason})"
    if outcome.method == METHOD_TELEGRAM:
        return f"test alert delivered via {outcome.method}"
    return (
        f"test alert delivered via {outcome.method} -- nothing was sent to Telegram "
        "(ALERT_METHOD is not telegram)"
    )


def _run_test_alert(args: argparse.Namespace, now: datetime) -> int:
    """``--test-alert``: send a labelled test message through the real alert path."""
    text = f"TEST ibkr health alert, sent {now:%Y-%m-%dT%H:%M:%SZ} -- no action needed"
    outcome = _deliver_alert(text, quiet=args.quiet)
    try:
        # There is no health status to report: "-" keeps the field count fixed.
        _append_alert_log(args.health_log_path, "test", "-", outcome, now)
    except Exception as exc:  # noqa: BLE001 - the outcome below must still be reported
        if not args.quiet:
            print(f"alert log write failed ({type(exc).__name__}); "
                  "the send outcome may be unrecorded", file=sys.stderr)
    if not args.quiet:
        print(_describe_test_alert(outcome))
    return 0 if outcome.delivered else 1


# CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-path", default=str(default_state_path()),
                    help="recorder heartbeat JSON (default: %(default)s)")
    ap.add_argument("--events-path", default=str(DEFAULT_EVENTS_PATH),
                    help="recorder connection-events JSONL (default: %(default)s)")
    ap.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES,
                    help="heartbeat age beyond which the recorder is STALE "
                         "(default: %(default)s = 3x the 20-min sweep interval)")
    ap.add_argument("--sync-runs-path", default=str(default_sync_runs_path()),
                    help="paper-sync run ledger JSONL (default: %(default)s)")
    ap.add_argument("--max-sync-age-hours", type=float, default=DEFAULT_MAX_SYNC_AGE_HOURS,
                    help="age of the last successful paper sync beyond which it is "
                         "SYNC_STALE (default: %(default)s = one daily cadence + 2h slack)")
    ap.add_argument("--health-log-path", default=str(DEFAULT_HEALTH_LOG_PATH),
                    help="durable append-only log: one status line per non-OK result plus one "
                         "ALERT_SENT / ALERT_FAILED line (with the delivery method) per "
                         "alert attempt (default: %(default)s)")
    ap.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT_PATH),
                    help="contract-universe snapshot file (H6): its age is reported "
                         "informationally as snapshot_age_days= on the OK message, never "
                         "a failure axis (default: %(default)s)")
    ap.add_argument("--alert-state-path", default=str(DEFAULT_ALERT_STATE_PATH),
                    help="JSON file remembering the last (recorder, sync) status pair and "
                         "when it was last alerted: alerts fire when the pair changes, "
                         "once a day while an outage lasts unchanged, and on every run "
                         "after a failed send until one gets through (default: %(default)s)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one clearly labelled test message through the normal alert "
                         "channel, say which method carried it (a log_only run says nothing "
                         "was sent to Telegram) and exit: 0 if delivered, 1 if not. Runs no "
                         "health check and neither reads nor writes the alert state file")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout (alerts still fire)")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.test_alert:
        return _run_test_alert(args, now)

    recorder_status, recorder_message = run_check(
        args.state_path, args.events_path, args.max_age_minutes, now=now,
        snapshot_path=args.snapshot_path,
    )
    sync_status, sync_message = _check_sync_freshness(
        args.sync_runs_path, args.max_sync_age_hours, now,
    )
    status, message = _combine_axes(recorder_status, recorder_message, sync_status, sync_message)

    if not args.quiet:
        print(message)
    if status != "OK":
        try:
            _append_health_log(args.health_log_path, status, message, now=now)
        except Exception as exc:  # noqa: BLE001 - the alert attempt below must still run
            if not args.quiet:
                print(f"_append_health_log failed ({type(exc).__name__}); "
                      "durable record may be lost", file=sys.stderr)
    try:
        _alert_on_change(args, (recorder_status, sync_status), status, message, now)
    except Exception as exc:  # noqa: BLE001 - the exit code must survive the alert path
        if not args.quiet:
            print(f"alert handling failed ({type(exc).__name__}); "
                  "the alert may not have been sent", file=sys.stderr)
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
