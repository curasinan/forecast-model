"""Health probe for the IBKR quote recorder + paper-book sync (migration
spec Stage 2.2, audit findings H3/M6 + operator-mandated sync check).

Read-only and cron / Task-Scheduler friendly: it inspects the recorder's
heartbeat state file, the connection-events JSONL, today's parquet
partition, and the paper-sync run ledger (Task 1.6's
``ibkr_sync_runs.jsonl``). It never touches the Gateway and never imports
ib_async, so it is safe to run at any time and finishes in well under 5
seconds.

Two independent axes are checked and combined into one status/message:

Recorder-heartbeat axis (the message is a single ASCII line):

    OK        exit 0  heartbeat fresh, data flow healthy; reports age,
                       rows/universe, today's parquet count, and the
                       contract-universe snapshot's age in days (H6,
                       informational only -- never a failure axis)
    STALE     exit 1  heartbeat older than --max-age-minutes
    CRITICAL  exit 1  state file missing or unreadable
    DOWN      exit 1  last event is an unexpected disconnect, no reconnect yet
    DEGRADED  exit 1  heartbeat fresh but rows < 50% of universe_size (M6:
                       a fresh heartbeat alone does not prove data is
                       actually flowing -- the recorder can tick on time
                       while barely collecting anything)

Sync axis (H3-adjacent, operator-approved 2026-08-25): the daily 17:30
paper-book sync dies with exit 1 when the Gateway is logged out, and
nothing caught that before this check -- a silent sync outage would
recreate audit finding C1 (fills landing unrecorded) for as long as it
lasts.

    SYNC_STALE  exit 1  the last "ok": true line in --sync-runs-path is
                        older than --max-sync-age-hours (default 26h = one
                        daily cadence + 2h slack), or the file is missing,
                        or it has no successful line

Recorder-axis statuses (CRITICAL/DOWN/STALE/DEGRADED) outrank SYNC_STALE in
the combined status, but ANY failing axis exits 1, and when both axes fail
the message mentions both.

Alerting -- on state changes, not on every unhealthy run (2026-10-03: the
2026-09-29..10-03 outage produced ~15 identical DOWN messages a day until the
operator stopped reading them). The alert state is the pair
(recorder_status, sync_status), remembered between runs in --alert-state-path
(default logs/ibkr_health_alert_state.json; written atomically; statuses, UTC
timestamps and one bool only -- never a token, a chat id or any environment
value). A run sends one alert through monitoring.heartbeat.send_alert (which
honors the ALERT_METHOD / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars)
when, and only when:

    transition  the pair differs from the stored one: an outage starts,
                recovers or changes kind. "[OK -> DOWN] ...",
                "[DOWN+SYNC_STALE -> SYNC_STALE] ...", and for a recovery
                "[DOWN -> OK] recovered after 4d 12h: ..."
    reminder    the pair is unchanged and still unhealthy, and the last
                DELIVERED alert is at least 24 hours old.
                "[still DOWN, 2d 3h] ..." (time since the outage began)
    retry       the pair is unchanged and unhealthy, and the previous send
                attempt was not delivered: tried again on this run whatever
                its age, because the network is often down exactly during an
                outage

An unchanged healthy run sends nothing and writes nothing. A missing,
unreadable or corrupt state file reads as "healthy, nothing alerted yet": an
unhealthy run then alerts and a healthy run stays silent -- the failure mode
is a repeated alert, never a missing one. A failure writing the state file or
the log never changes the exit code or stops the alert attempt. All of this
follows from decide_alert(), a pure function of (previous state, current
pair, now); a run reads the clock once.

Durable sink (H3): every non-OK combined result appends one line
("<utc-iso> <STATUS> <message>") to --health-log-path (default
logs/ibkr_health.log) via a direct file append independent of logging
configuration -- a handlerless logger silently dropping the alert to a
discarded stderr must never be the only trace a failure left behind. A
healthy result appends no such line. In addition every send attempt appends
one line, "<utc-iso> ALERT_SENT <kind> <STATUS>" or "<utc-iso> ALERT_FAILED
<kind> <STATUS> <reason>" (kind: transition | reminder | retry | test;
reason: a fixed phrase or an exception CLASS NAME, never exception text), so
a successful send leaves a trace too, and a recovery -- the one healthy run
that sends -- is recorded. The token used by send_alert's Telegram path is
never written to this file, to the alert state file or to any log line (see
monitoring.heartbeat).

--test-alert sends one clearly labelled test message through the same
send_alert path, appends its ALERT_SENT / ALERT_FAILED line (kind test),
prints the outcome and exits 0 if it was delivered, 1 if not. It runs no
health check and neither reads nor writes the alert state file, so a test
never disturbs a live outage's bookkeeping.

Motivating incident (2026-08-16): Gateway's nightly auto-logoff silently
stalled collection for 15 hours while the recorder retried every 60s. A
scheduled run of this probe turns that silence into an alert.

Usage:
    python scripts/check_ibkr_recorder.py
    python scripts/check_ibkr_recorder.py --max-age-minutes 90 --quiet
    python scripts/check_ibkr_recorder.py --max-sync-age-hours 30
    python scripts/check_ibkr_recorder.py --test-alert
"""

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.heartbeat import send_alert  # noqa: E402
from src.utils.dates import parse_iso8601  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS_PATH = REPO_ROOT / "logs" / "ibkr_events.jsonl"
DEFAULT_HEALTH_LOG_PATH = REPO_ROOT / "logs" / "ibkr_health.log"
DEFAULT_ALERT_STATE_PATH = REPO_ROOT / "logs" / "ibkr_health_alert_state.json"
# H6: same file the recorder seeds from (src/venues/ibkr/recorder.py); this
# probe only surfaces its age informationally, it never fails on it.
DEFAULT_SNAPSHOT_PATH = REPO_ROOT / "data" / "ibkr_universe_live.json"
# 3x the 20-minute sweep cadence: one sweep takes ~13 min, so a healthy
# recorder can never legitimately be more than ~2 intervals behind.
DEFAULT_MAX_AGE_MINUTES = 60.0
# One daily sync cadence (17:30 -> 17:30) + 2h slack for a late run.
DEFAULT_MAX_SYNC_AGE_HOURS = 26.0
# M6: a fresh heartbeat that is barely collecting anything is not "healthy"
# data flow -- half the known universe is a conservative floor (a normal
# sweep sees ~all of it; ForecastEx thin hours still clear this easily).
DEGRADED_ROWS_FRACTION = 0.5
EVENTS_TAIL_LINES = 5
SYNC_RUNS_FILENAME = "ibkr_sync_runs.jsonl"

# "gave_up_window" is the gateway's "still down after the expected nightly
# restart window closed" signal -- operationally a DOWN, not an expected blip.
_DISCONNECT_EVENTS = {
    "disconnect", "disconnected", "unexpected_disconnect", "connection_lost",
    "gave_up_window",
}
_RECONNECT_EVENTS = {"reconnect", "reconnected", "connect", "connected"}


def default_state_path() -> Path:
    """Heartbeat path under the recorder's data root.

    Mirrors IBKRSettings.resolved_data_root (IBKR_DATA_ROOT, else
    ~/quant_data/ibkr_ticks) WITHOUT constructing IBKRSettings: the settings
    class fails loudly when IBKR_PORT / IBKR_TRADING_MODE are unset (by
    design, spec Stage 7), and a monitoring probe must keep working in a bare
    scheduler env. NOT %LOCALAPPDATA%: a packaged app virtualises AppData, so
    probe and recorder would read two different files (2026-08-25).
    """
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path.home() / "quant_data" / "ibkr_ticks"
    return root / "state" / "recorder_state.json"


def default_sync_runs_path() -> Path:
    """Paper-sync run ledger under the same data root as ``default_state_path``.

    Mirrors scripts/sync_ibkr_paper.py's own derivation
    (``<resolved_data_root>/paper/ibkr_sync_runs.jsonl``) without importing
    IBKRSettings, for the same bare-scheduler-env reason as
    ``default_state_path``.
    """
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path.home() / "quant_data" / "ibkr_ticks"
    return root / "paper" / SYNC_RUNS_FILENAME


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


def _snapshot_age_days(snapshot_path: Path, now: datetime) -> int | None:
    """Whole days since the universe snapshot's mtime, or None if missing/unreadable.

    H6: informational only -- this NEVER becomes a failure axis or changes
    the exit code. The recorder (src/venues/ibkr/recorder.py) already logs a
    WARNING at seed time once this crosses 35 days; the probe just surfaces
    the same number in the OK message so an operator sees decay without
    digging through recorder logs.
    """
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
    """Run the recorder-heartbeat axis. Returns (status, one-line ASCII message).

    status is one of "OK" / "STALE" / "CRITICAL" / "DOWN" / "DEGRADED"; only
    "OK" is healthy. Pure function of the filesystem -- no network, no side
    effects. (The sync-run axis is a separate check; see
    ``_check_sync_freshness``.) ``snapshot_path`` only feeds the
    informational ``snapshot_age_days=`` field on the OK message (H6); it
    never affects the returned status.
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

    # M6: heartbeat age alone proves the recorder process is alive, not that
    # it is actually collecting data -- a sweep that returns almost nothing
    # from a normal-sized universe is a distinct failure mode. Guard against
    # a missing/zero universe_size: there is nothing to divide by, and that
    # is not itself evidence of a problem (older state files predate this
    # field), so it must never manufacture a false DEGRADED.
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
    """Return (timestamp of the LAST "ok": true line, file_exists).

    The ledger (scripts/sync_ibkr_paper.py) is append-only in chronological
    order, so the last matching line wins over any earlier one. Lines that
    are not valid JSON, not an object, or have no parseable ``ts_utc`` are
    tolerated by skipping just that line -- a single corrupt line must never
    crash the probe or hide an otherwise-valid successful run elsewhere in
    the file.
    """
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
    """Run the paper-sync axis. Returns ("OK", msg) or ("SYNC_STALE", msg).

    SYNC_STALE when the run ledger is missing, unreadable, has no
    "ok": true line, or the last one is older than max_sync_age_hours. Pure
    function of the filesystem -- no network, no side effects.
    """
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
    """Combine the two axes into one (status, message).

    Recorder-heartbeat statuses (CRITICAL/DOWN/STALE/DEGRADED) outrank
    SYNC_STALE in the reported status, but either axis failing exits 1 (the
    caller treats any non-"OK" combined status as unhealthy), and when both
    fail the message names both so an operator sees the whole picture, not
    just whichever axis happened to win.
    """
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
    """Durable sink (H3): append one line for a non-OK result.

    Direct file append -- no dependence on logging configuration (module
    logging, handlers, propagation). A handlerless logger discarding an
    alert to stderr that Task Scheduler throws away must never be the only
    record a failure left behind. Never called for "OK" (nothing to record;
    OK appends nothing). Never write the Telegram token here -- ``message``
    only ever carries status text, never env values.
    """
    now = now or datetime.now(timezone.utc)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{now.isoformat()} {status} {message}\n")


def _append_alert_log(
    log_path: Path | str,
    kind: str,
    status: str,
    delivered: bool,
    reason: str | None,
    now: datetime,
) -> None:
    """Durable record of one send attempt, beside the per-run status lines:

        <utc-iso> ALERT_SENT <kind> <status>
        <utc-iso> ALERT_FAILED <kind> <status> <reason>

    ``kind`` is transition | reminder | retry | test. ``reason`` is a fixed
    phrase or an exception CLASS NAME -- never exception text, which can quote
    the Telegram URL and with it the bot token. A successful send used to leave
    no durable trace at all; a recovery (the one healthy run that sends) is
    recorded here too.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{now.isoformat()} {'ALERT_SENT' if delivered else 'ALERT_FAILED'} {kind} {status}"
    if not delivered:
        line += f" {reason}"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------------- #
# Alert state: alert on state changes, not on every unhealthy run
# --------------------------------------------------------------------------- #

# The alert state is the (recorder_status, sync_status) pair; all-OK is healthy.
OK_PAIR = ("OK", "OK")
# While an outage lasts unchanged, the operator is reminded once per this
# interval, counted from the last alert that was actually delivered.
ALERT_REMINDER_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class AlertState:
    """What the previous runs told the operator: the persisted alert state.

    ``since`` is when the pair last changed; ``outage_since`` when it last
    left OK/OK (None while healthy). ``last_alert`` / ``last_alert_ok`` are the
    time and outcome of the most recent send attempt. Only statuses, UTC
    timestamps and one bool live here -- never an environment value.
    """

    recorder_status: str = "OK"
    sync_status: str = "OK"
    since: datetime | None = None
    last_alert: datetime | None = None
    last_alert_ok: bool | None = None
    outage_since: datetime | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.recorder_status, self.sync_status)


# No state file, or an unreadable one: healthy, and nothing alerted yet. This
# fails toward alerting -- an unhealthy run alerts, a healthy run stays silent.
NO_ALERT_STATE = AlertState()


@dataclass(frozen=True)
class AlertDecision:
    """``kind`` is "transition", "reminder" or "retry" (None: stay silent).
    ``prefix`` leads the alert text. ``state`` is the successor state before
    the send attempt is recorded (see ``record_alert_attempt``)."""

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


def decide_alert(previous: AlertState, pair: tuple[str, str], now: datetime) -> AlertDecision:
    """Whether this run should alert, and what the successor state looks like.

    A pure function of (previous state, current pair, now): the 24 h
    boundary and every branch are testable without a clock.

    - the pair differs from the stored one -> "transition" (an outage starts,
      recovers, or changes kind)
    - unchanged and all-OK -> silent
    - unchanged, unhealthy, and the last send attempt was not delivered (or
      nothing was ever recorded) -> "retry", whatever its age: the network is
      often down exactly during an outage
    - unchanged, unhealthy, and the last DELIVERED alert is at least
      ``ALERT_REMINDER_INTERVAL`` old -> "reminder"
    - otherwise silent
    """
    if pair != previous.pair:
        kind = "transition"
    elif pair == OK_PAIR:
        return AlertDecision(None, "", previous)
    elif previous.last_alert_ok is not True or previous.last_alert is None:
        kind = "retry"
    elif now - previous.last_alert >= ALERT_REMINDER_INTERVAL:
        kind = "reminder"
    else:
        return AlertDecision(None, "", previous)

    # When the current outage began: carried over while the pair stays
    # unhealthy, now when it starts (or when the state did not say).
    outage_start = (previous.outage_since if previous.pair != OK_PAIR else None) or now
    elapsed = _format_duration(now - outage_start)
    if kind != "transition":
        return AlertDecision(kind, f"[still {_pair_label(pair)}, {elapsed}]", previous)

    # Nothing has been sent for the new pair yet: last_alert / last_alert_ok
    # stay unset until record_alert_attempt stamps the outcome.
    successor = AlertState(
        pair[0], pair[1], since=now, outage_since=None if pair == OK_PAIR else outage_start
    )
    before = _pair_label(previous.pair)
    if pair == OK_PAIR:
        prefix = f"[{before} -> OK] recovered after {elapsed}:"
    else:
        prefix = f"[{before} -> {_pair_label(pair)}]"
    return AlertDecision(kind, prefix, successor)


def record_alert_attempt(state: AlertState, now: datetime, delivered: bool) -> AlertState:
    """``state`` with the send attempt just made stamped on it."""
    return replace(state, last_alert=now, last_alert_ok=delivered)


# Statuses are upper-case identifiers (OK, STALE, SYNC_STALE, ...). A stored
# status that is anything else makes the file corrupt: it would otherwise reach
# the alert text and the durable log, where an embedded newline forges a line.
_STATUS_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,31}")


def _alert_state_from_json(raw: object) -> AlertState | None:
    """The state a decoded JSON document describes, or None when it is not a
    well-formed one (wrong shape, wrong types, an unparseable timestamp, or an
    unhealthy pair that does not say when its outage began)."""
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
    )
    if state.pair != OK_PAIR and state.outage_since is None:
        return None
    return state


def load_alert_state(path: Path | str) -> AlertState:
    """The persisted alert state, or ``NO_ALERT_STATE``. Never raises.

    A missing, unreadable or corrupt file all read as "healthy, nothing
    alerted yet": the next unhealthy run alerts, the next healthy run stays
    silent. The cost of a bad file is at most one repeated alert, never a
    swallowed one and never a crash of the probe.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, RecursionError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError;
        # RecursionError is what json raises for a pathologically nested file.
        return NO_ALERT_STATE
    return _alert_state_from_json(raw) or NO_ALERT_STATE


def _iso(stamp: datetime | None) -> str | None:
    return None if stamp is None else stamp.isoformat()


def save_alert_state(path: Path | str, state: AlertState) -> None:
    """Persist ``state`` atomically: a complete temp file in the same
    directory, then ``os.replace`` over the target, so a reader (or a crash)
    never sees a half-written file. The temp file is removed if anything
    fails; the error propagates (the caller treats it as best-effort).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorder_status": state.recorder_status,
        "sync_status": state.sync_status,
        "since_utc": _iso(state.since),
        "last_alert_utc": _iso(state.last_alert),
        "last_alert_ok": state.last_alert_ok,
        "outage_since_utc": _iso(state.outage_since),
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


# Reason recorded in the durable log when send_alert answers "not delivered"
# (it already logged the specifics, scrubbed; its answer is just a bool).
_NOT_DELIVERED = "not_delivered"


def _deliver_alert(text: str, *, quiet: bool) -> tuple[bool, str | None]:
    """Send ``text`` through send_alert. Returns (delivered, reason): reason is
    None when delivered, else a fixed phrase or an exception CLASS NAME.

    Never raises. Only an explicit True counts as delivered: any other answer
    (None from an old stand-in, an unexpected object) is treated as undelivered
    and so retried -- an alert may be repeated, never lost.
    """
    try:
        delivered = send_alert(text) is True
    except Exception as exc:  # noqa: BLE001 - the exit code must survive any alert failure
        if not quiet:
            # Class name only (final-review C-2): this stderr is appended to
            # logs/ibkr_health_task.log, and an exception from the Telegram
            # path can quote the request URL -- bot token included.
            print(f"send_alert failed ({type(exc).__name__}); alert may be lost",
                  file=sys.stderr)
        return False, type(exc).__name__
    return (True, None) if delivered else (False, _NOT_DELIVERED)


def _alert_on_change(
    args: argparse.Namespace,
    pair: tuple[str, str],
    status: str,
    message: str,
    now: datetime,
) -> None:
    """Alert when the (recorder, sync) pair changed, remind while an outage
    lasts, retry what did not get through (see ``decide_alert``), and record
    the attempt durably and in the alert state file.

    Every step is independently best-effort, like the status-line sink in
    ``main``: a state-file problem must never stop the alert, and a failing
    log or state write must never change the exit code. The worst a broken
    state file costs is a repeated alert. Notes go to stderr by exception CLASS
    NAME only.
    """
    state_path = Path(args.alert_state_path)
    try:
        previous = load_alert_state(state_path)
    except Exception:  # noqa: BLE001 - it should not raise; if it does, still alert
        previous = NO_ALERT_STATE
    decision = decide_alert(previous, pair, now)
    if decision.kind is None:
        return

    delivered, reason = _deliver_alert(f"{decision.prefix} {message}", quiet=args.quiet)
    try:
        _append_alert_log(args.health_log_path, decision.kind, status, delivered, reason, now)
    except Exception as exc:  # noqa: BLE001 - the state update below must still run
        if not args.quiet:
            print(f"alert log write failed ({type(exc).__name__}); "
                  "the send outcome may be unrecorded", file=sys.stderr)
    try:
        save_alert_state(state_path, record_alert_attempt(decision.state, now, delivered))
    except Exception as exc:  # noqa: BLE001 - exit code must survive a state-file failure
        if not args.quiet:
            print(f"alert state write failed ({type(exc).__name__}); "
                  "the next run may repeat this alert", file=sys.stderr)


def _run_test_alert(args: argparse.Namespace, now: datetime) -> int:
    """``--test-alert``: send a clearly labelled test message through the same
    path a real alert takes, record the attempt in the durable log (kind
    ``test``) and report the outcome: exit 0 when delivered, 1 when not.

    No health check runs and the alert state file is neither read nor written,
    so a test never disturbs a live outage's bookkeeping (no phantom
    transition, no reset reminder clock).
    """
    text = f"TEST ibkr health alert, sent {now:%Y-%m-%dT%H:%M:%SZ} -- no action needed"
    delivered, reason = _deliver_alert(text, quiet=args.quiet)
    try:
        # There is no health status to report: "-" keeps the field count fixed.
        _append_alert_log(args.health_log_path, "test", "-", delivered, reason, now)
    except Exception as exc:  # noqa: BLE001 - the outcome below must still be reported
        if not args.quiet:
            print(f"alert log write failed ({type(exc).__name__}); "
                  "the send outcome may be unrecorded", file=sys.stderr)
    if not args.quiet:
        print("test alert delivered" if delivered else f"test alert NOT delivered ({reason})")
    return 0 if delivered else 1


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
    ap.add_argument("--sync-runs-path", default=str(default_sync_runs_path()),
                    help="paper-sync run ledger JSONL (default: %(default)s)")
    ap.add_argument("--max-sync-age-hours", type=float, default=DEFAULT_MAX_SYNC_AGE_HOURS,
                    help="age of the last successful paper sync beyond which it is "
                         "SYNC_STALE (default: %(default)s = one daily cadence + 2h slack)")
    ap.add_argument("--health-log-path", default=str(DEFAULT_HEALTH_LOG_PATH),
                    help="durable append-only log: one status line per non-OK result plus one "
                         "ALERT_SENT / ALERT_FAILED line per alert attempt "
                         "(default: %(default)s)")
    ap.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT_PATH),
                    help="contract-universe snapshot file (H6): its age is reported "
                         "informationally as snapshot_age_days= on the OK message, never "
                         "a failure axis (default: %(default)s)")
    ap.add_argument("--alert-state-path", default=str(DEFAULT_ALERT_STATE_PATH),
                    help="JSON file remembering the last (recorder, sync) status pair and "
                         "when it was last alerted: alerts fire when the pair changes, "
                         "once a day while an outage lasts unchanged, and again after a "
                         "failed send (default: %(default)s)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one clearly labelled test message through the normal alert "
                         "channel and exit: 0 if delivered, 1 if not. Runs no health check "
                         "and neither reads nor writes the alert state file")
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
    # The channels are independently best-effort: a sink write failure
    # (permission-denied logs/, full disk, an AV file lock -- all plausible
    # over 365 unattended days) must never prevent the alert attempt, and an
    # alert or alert-state failure must never prevent the sink write. Each is
    # wrapped in its own try/except so one channel's exception can't propagate
    # out of main() and skip the next -- an uncaught exception here would
    # still exit 1 (CPython's default), but only by accident, and only after
    # silently dropping whichever channel came second. The exit code stays
    # deliberate: 0 only when everything is healthy.
    if status != "OK":
        try:
            _append_health_log(args.health_log_path, status, message, now=now)
        except Exception as exc:  # noqa: BLE001 - the alert attempt below must still run
            if not args.quiet:
                print(f"_append_health_log failed ({exc}); durable record may be lost",
                      file=sys.stderr)
    # Alert on a state change (including the recovery of a healthy run), not on
    # every unhealthy run; nothing in here can change the exit code.
    _alert_on_change(args, (recorder_status, sync_status), status, message, now)
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
