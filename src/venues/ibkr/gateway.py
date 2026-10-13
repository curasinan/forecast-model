"""Gateway session supervisor (migration spec Stage 2.2)."""

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

from src.venues.ibkr.clients import ClientIdInUseError, connect_ib
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
    expected: bool
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
    """Parse 'HH:MM-HH:MM' (LOCAL clock) into (start, end) minutes of day."""
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
    """Owns one Gateway connection for one role and supervises its liveness."""

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

    def on_event(self, sink: Callable[[ConnectionEvent], None]) -> None:
        """Register an event sink; sink exceptions are logged, never raised."""
        self._sinks.append(sink)

    def on_reconnect(self, hook: Callable[[], None]) -> None:
        """Register a hook that runs after each successful RE-connect."""
        self._reconnect_hooks.append(hook)

    def ensure_connected(self) -> Any:
        """Return a healthy ib_async IB, reconnecting (with backoff) if needed."""
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

    def _heartbeat_error(self, ib: Any) -> str | None:
        """None when the session answers; otherwise a short failure summary."""
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
            except ClientIdInUseError:
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
