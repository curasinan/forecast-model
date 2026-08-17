"""Gateway session supervisor (migration spec Stage 2.2).

Deliberately THREAD-LESS and synchronous: the recorder is a sync sweep
loop with a 20-minute cadence, so a background heartbeat thread would
add cross-thread races (two owners of one ib_async socket) for no
benefit. Instead the driving process probes health right before each
sweep (``ensure_connected``) and routes mid-sweep connection failures
back through the same call. Worst case a dead session goes unnoticed
for one interval, which the 60s request timeout inside a sweep already
bounds far tighter.

Liveness is an ACTIVE heartbeat, not TCP liveness: after Gateway's
nightly auto-restart stalled collection for 15 hours (2026-08-16) the
socket stayed "connected" while every request went unanswered.
``isConnected()`` alone is therefore never trusted; a healthy session
must answer ``reqCurrentTime()`` within a short timeout.

Expected-window classification: IB Gateway with "Auto restart" drops
the API around ~midnight US-Eastern daily. ``settings.restart_window_local``
expresses that window in THIS machine's LOCAL clock ('HH:MM-HH:MM', may
cross midnight); events emitted inside it get ``expected=True`` so
monitoring can separate planned restarts from real outages. When an
outage overlaps the window but the Gateway is still unreachable after
the window closes, a single ``gave_up_window`` event fires -- with Auto
restart, that usually means the weekly manual re-login is due.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from src.venues.ibkr.clients import connect_ib
from src.venues.ibkr.settings import IBKRSettings

logger = logging.getLogger(__name__)

BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.2  # each sleep is base * uniform(0.8, 1.2)
HEARTBEAT_TIMEOUT_S = 5.0
DEFAULT_EVENTS_PATH = Path("logs") / "ibkr_events.jsonl"

EventKind = Literal["disconnect", "reconnect", "heartbeat_miss", "gave_up_window"]


@dataclass(frozen=True)
class ConnectionEvent:
    """One connection-state transition, for sinks (JSONL, logging, alerting)."""

    kind: EventKind
    ts_utc: str
    detail: str
    expected: bool  # True when emitted inside settings.restart_window_local
    downtime_seconds: float | None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minute_of_day(text: str) -> int:
    hour_s, minute_s = text.strip().split(":")
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time of day out of range: {text!r}")
    return hour * 60 + minute


def parse_restart_window(spec: str) -> tuple[int, int]:
    """Parse 'HH:MM-HH:MM' (LOCAL clock) into (start, end) minutes of day.

    start > end means the window crosses midnight (e.g. '23:30-00:45').
    Both boundary minutes are inside the window.
    """
    try:
        start_s, end_s = spec.strip().split("-")
        return _minute_of_day(start_s), _minute_of_day(end_s)
    except ValueError as exc:
        raise ValueError(
            f"restart_window_local must be 'HH:MM-HH:MM' (local clock), got {spec!r}"
        ) from exc


def in_restart_window(now_local: datetime, window: tuple[int, int]) -> bool:
    """True when now_local falls inside the window (boundaries inclusive)."""
    start, end = window
    minute = now_local.hour * 60 + now_local.minute
    if start <= end:
        return start <= minute <= end
    return minute >= start or minute <= end  # crosses midnight


def append_event_jsonl(event: ConnectionEvent, path: str | Path = DEFAULT_EVENTS_PATH) -> Path:
    """Append one event as a JSON line (built-in structured-event sink)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event)) + "\n")
    return path


class GatewaySession:
    """Owns one Gateway connection for one role and supervises its liveness.

    The driving loop calls ``ensure_connected()`` before each unit of work;
    everything else (heartbeat, backoff, events, resubscription hooks) hangs
    off that single entry point. ``connect_validator`` runs after every
    successful connect and its exceptions PROPAGATE out of the retry loop:
    it is for configuration errors (e.g. the recorder's account-mode
    cross-check) that must kill the process, not be retried.

    The keyword-only ``connect``/``sleep``/``clock``/``local_now`` seams
    exist for unit tests; production code never passes them.
    """

    def __init__(
        self,
        settings: IBKRSettings,
        role: str,
        *,
        connect_validator: Callable[[Any], None] | None = None,
        connect: Callable[[str, IBKRSettings], Any] = connect_ib,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        local_now: Callable[[], datetime] = datetime.now,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
    ):
        self.settings = settings
        self.role = role
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._connect = connect
        self._connect_validator = connect_validator
        self._sleep = sleep
        self._clock = clock
        self._local_now = local_now
        self._window = parse_restart_window(settings.restart_window_local)
        self._sinks: list[Callable[[ConnectionEvent], None]] = []
        self._reconnect_hooks: list[Callable[[], None]] = []
        self._ib: Any = None
        self._ever_connected = False
        self._outage_started: float | None = None
        self._outage_touched_window = False
        self._gave_up_emitted = False

    # -- registration --------------------------------------------------------
    def on_event(self, sink: Callable[[ConnectionEvent], None]) -> None:
        """Register an event sink; sink exceptions are logged, never raised."""
        self._sinks.append(sink)

    def on_reconnect(self, hook: Callable[[], None]) -> None:
        """Register a resubscription hook (market data subscriptions, order
        status watches, cache flushes). Every hook runs after each successful
        RE-connect -- not after the first-ever connect, where there is nothing
        to re-establish. Hook exceptions are logged, never raised."""
        self._reconnect_hooks.append(hook)

    # -- health --------------------------------------------------------------
    def ensure_connected(self) -> Any:
        """Return a healthy ib_async IB, reconnecting (with backoff) if needed.

        Healthy = socket connected AND an active ``reqCurrentTime()`` answered
        within ``heartbeat_timeout_s``. Socket-alive alone is NOT enough: the
        2026-08-16 auto-logoff stall kept the TCP session up while every
        request hung.
        """
        ib = self._ib
        if ib is not None:
            if not ib.isConnected():
                self._begin_outage("socket reports disconnected")
            else:
                error = self._heartbeat_error(ib)
                if error is None:
                    return ib
                self._emit("heartbeat_miss", f"active heartbeat failed: {error}")
                self._begin_outage(f"active heartbeat failed: {error}")
            self._drop(ib)
            self._ib = None
        return self._reconnect_loop()

    def close(self) -> None:
        ib, self._ib = self._ib, None
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                logger.warning("disconnect during close failed", exc_info=True)

    # -- internals -----------------------------------------------------------
    def _heartbeat_error(self, ib: Any) -> str | None:
        """None when the session answers; otherwise a short failure summary.

        The short timeout is applied via ib_async's class-level RequestTimeout
        (the only knob its sync wrappers expose) and restored afterwards.
        """
        cls = type(ib)
        saved = getattr(cls, "RequestTimeout", None)
        try:
            if saved is not None:
                cls.RequestTimeout = self.heartbeat_timeout_s
            ib.reqCurrentTime()
            return None
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            if saved is not None:
                cls.RequestTimeout = saved

    def _begin_outage(self, detail: str) -> None:
        """Mark the start of an outage (idempotent) and emit ``disconnect``."""
        if self._outage_started is not None:
            return
        self._outage_started = self._clock()
        self._outage_touched_window = self._in_window()
        self._gave_up_emitted = False
        self._emit("disconnect", detail)

    def _reconnect_loop(self) -> Any:
        """Retry ``connect`` with 1s->60s doubling backoff and +-20% jitter."""
        delay = BACKOFF_INITIAL_S
        attempts = 0
        while True:
            attempts += 1
            try:
                ib = self._connect(self.role, self.settings)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._begin_outage(f"connect failed: {exc}")
                self._note_window_progress()
                logger.warning("connect attempt %d failed (%s); retrying in ~%.0fs",
                               attempts, exc, delay)
                self._sleep(delay * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER))
                delay = min(delay * 2.0, BACKOFF_CAP_S)
                continue
            if self._connect_validator is not None:
                self._connect_validator(ib)  # config errors propagate, never retried
            self._ib = ib
            if self._outage_started is not None:
                downtime = round(self._clock() - self._outage_started, 3)
                self._outage_started = None
                self._emit("reconnect", f"connected after {attempts} attempt(s)",
                           downtime_seconds=downtime)
            if self._ever_connected:
                self._run_reconnect_hooks()
            self._ever_connected = True
            return ib

    def _note_window_progress(self) -> None:
        """Emit ``gave_up_window`` once when an outage outlives the window."""
        if self._in_window():
            self._outage_touched_window = True
        elif self._outage_touched_window and not self._gave_up_emitted:
            self._gave_up_emitted = True
            elapsed = None
            if self._outage_started is not None:
                elapsed = round(self._clock() - self._outage_started, 3)
            self._emit(
                "gave_up_window",
                "restart window closed but Gateway is still unreachable; "
                "the weekly manual re-login is probably due",
                downtime_seconds=elapsed,
            )

    def _run_reconnect_hooks(self) -> None:
        for hook in self._reconnect_hooks:
            try:
                hook()
            except Exception:
                logger.exception("reconnect hook %r failed", hook)

    def _in_window(self) -> bool:
        return in_restart_window(self._local_now(), self._window)

    def _emit(self, kind: EventKind, detail: str,
              downtime_seconds: float | None = None) -> None:
        event = ConnectionEvent(
            kind=kind,
            ts_utc=_utc_now_iso(),
            detail=detail,
            expected=self._in_window(),
            downtime_seconds=downtime_seconds,
        )
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:
                logger.exception("event sink %r failed", sink)

    @staticmethod
    def _drop(ib: Any) -> None:
        try:
            ib.disconnect()
        except Exception:
            pass
