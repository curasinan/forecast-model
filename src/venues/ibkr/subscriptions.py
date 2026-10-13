"""Market-data subscription-line manager (migration spec Stage 5.2)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

log = logging.getLogger(__name__)


class LineBudgetExceeded(RuntimeError):
    """Raised when a subscription cannot be granted and nothing is evictable."""


@dataclass(frozen=True)
class Decision:
    """What the adapter should do about a :meth:`SubscriptionManager.request`."""

    action: str
    market_id: Hashable
    evict: Hashable | None = None


@dataclass
class _Line:
    """Book-keeping for one active market-data line."""

    priority: int
    seq: int  # recency stamp; higher == more recently touched


class _TokenBucket:
    """Blocking token bucket of depth one; refills at ``rate`` tokens/sec."""

    _CAPACITY = 1.0

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ):
        self._rate = rate
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._tokens = self._CAPACITY
        self._last = clock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        self._last = now
        if elapsed > 0.0:
            self._tokens = min(self._CAPACITY, self._tokens + elapsed * self._rate)

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                now = self._clock()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            log.warning(
                "subscription pacing: blocked for %.3fs (rate=%.3f/s)",
                wait,
                self._rate,
            )
            self._sleeper(wait)


class SubscriptionManager:
    """Line-budget + subscribe-rate manager for streaming market data."""

    def __init__(
        self,
        max_lines: int,
        subscribe_rate_per_sec: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_lines < 1:
            raise ValueError(f"max_lines must be >= 1, got {max_lines}")
        rate = subscribe_rate_per_sec if subscribe_rate_per_sec is not None else max_lines / 2.0
        if rate <= 0.0:
            raise ValueError(f"subscribe_rate_per_sec must be > 0, got {rate}")

        self.max_lines = max_lines
        self.subscribe_rate_per_sec = rate

        self._lock = threading.Lock()
        self._lines: dict[Hashable, _Line] = {}
        self._seq = 0
        self._evictions = 0
        self._rejections = 0
        self._bucket = _TokenBucket(rate, clock, sleeper)

    # -- rate gate ----------------------------------------------------------

    def acquire_slot(self) -> None:
        """Block until the subscribe rate permits opening another line."""
        self._bucket.acquire()

    # -- capacity accounting ------------------------------------------------

    def request(self, market_id: Hashable, priority: int = 0) -> Decision:
        """Decide how to satisfy a subscription request for ``market_id``."""
        with self._lock:
            existing = self._lines.get(market_id)
            if existing is not None:
                # Already subscribed: refresh recency/priority, no venue call.
                existing.priority = priority
                existing.seq = self._next_seq()
                return Decision(action="noop", market_id=market_id)

            if len(self._lines) < self.max_lines:
                self._lines[market_id] = _Line(priority=priority, seq=self._next_seq())
                return Decision(action="subscribe", market_id=market_id)

            victim = self._pick_victim(priority)
            if victim is None:
                self._rejections += 1
                held = min(line.priority for line in self._lines.values())
                log.warning(
                    "subscription manager: rejecting %r (priority=%d): all %d "
                    "lines held at priority >= %d",
                    market_id,
                    priority,
                    self.max_lines,
                    priority,
                )
                raise LineBudgetExceeded(
                    f"cannot subscribe {market_id!r} (priority={priority}): "
                    f"all {self.max_lines} lines held at priority >= {priority} "
                    f"(lowest active priority {held})"
                )

            evicted = self._lines.pop(victim)
            self._evictions += 1
            self._lines[market_id] = _Line(priority=priority, seq=self._next_seq())
            log.warning(
                "subscription manager: evicting %r (priority=%d) for %r "
                "(priority=%d) at capacity %d",
                victim,
                evicted.priority,
                market_id,
                priority,
                self.max_lines,
            )
            return Decision(action="subscribe", market_id=market_id, evict=victim)

    def release(self, market_id: Hashable) -> bool:
        """Free the line held by ``market_id``. Returns True if one was freed."""
        with self._lock:
            return self._lines.pop(market_id, None) is not None

    def touch(self, market_id: Hashable) -> bool:
        """Mark ``market_id`` as most-recently used. Returns True if active."""
        with self._lock:
            line = self._lines.get(market_id)
            if line is None:
                return False
            line.seq = self._next_seq()
            return True

    # -- introspection ------------------------------------------------------

    def active_ids(self) -> list[Hashable]:
        with self._lock:
            return list(self._lines)

    def is_active(self, market_id: Hashable) -> bool:
        with self._lock:
            return market_id in self._lines

    def free_lines(self) -> int:
        with self._lock:
            return self.max_lines - len(self._lines)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": len(self._lines),
                "capacity": self.max_lines,
                "evictions": self._evictions,
                "rejections": self._rejections,
            }

    # -- internals (call with self._lock held) ------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _pick_victim(self, priority: int) -> Hashable | None:
        """Lowest-priority, least-recently-touched active line below ``priority``, or None."""
        victim: Hashable | None = None
        best: tuple[int, int] | None = None  # (priority, seq); smaller wins
        for market_id, line in self._lines.items():
            if line.priority >= priority:
                continue
            candidate = (line.priority, line.seq)
            if best is None or candidate < best:
                best = candidate
                victim = market_id
        return victim
