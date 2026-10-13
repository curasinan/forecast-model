"""Client-side pacing for IBKR historical-data requests."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

IDENTICAL_WINDOW_S = 15.0
BURST_WINDOW_S = 2.0
BURST_LIMIT = 6
BUDGET_WINDOW_S = 600.0
BUDGET_LIMIT = 60
BID_ASK_WEIGHT = 2


@dataclass(frozen=True)
class RequestKey:
    """Identity of one historical-data request for pacing purposes."""

    contract_key: str
    exchange: str
    tick_type: str
    request_fingerprint: str

    @property
    def burst_key(self) -> tuple[str, str, str]:
        return (self.contract_key, self.exchange, self.tick_type)

    @property
    def weight(self) -> int:
        return BID_ASK_WEIGHT if self.tick_type == "BID_ASK" else 1


class HistoricalPacer:
    """Blocking, thread-safe rate limiter for historical requests."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_by_fingerprint: dict[str, float] = {}
        self._burst: dict[tuple[str, str, str], deque[float]] = {}
        self._budget: deque[tuple[float, int]] = deque()

    def acquire(self, key: RequestKey) -> None:
        """Block until ``key`` may be sent, then record it as sent."""
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                rule, wait = self._binding_rule(key, now)
                if wait <= 0.0:
                    self._record(key, now)
                    return
            log.warning(
                "historical pacing: blocked by %s for %.3fs "
                "(contract=%s exchange=%s tick_type=%s)",
                rule,
                wait,
                key.contract_key,
                key.exchange,
                key.tick_type,
            )
            self._sleeper(wait)

    # -- internals (call with self._lock held) ------------------------------

    def _prune(self, now: float) -> None:
        stale = now - IDENTICAL_WINDOW_S
        self._last_by_fingerprint = {
            fp: ts for fp, ts in self._last_by_fingerprint.items() if ts > stale
        }
        for bkey in list(self._burst):
            dq = self._burst[bkey]
            while dq and dq[0] <= now - BURST_WINDOW_S:
                dq.popleft()
            if not dq:
                del self._burst[bkey]
        while self._budget and self._budget[0][0] <= now - BUDGET_WINDOW_S:
            self._budget.popleft()

    def _binding_rule(self, key: RequestKey, now: float) -> tuple[str, float]:
        waits = (
            ("identical-request", self._identical_wait(key, now)),
            ("per-contract-burst", self._burst_wait(key, now)),
            ("ten-minute-budget", self._budget_wait(key, now)),
        )
        return max(waits, key=lambda item: item[1])

    def _identical_wait(self, key: RequestKey, now: float) -> float:
        last = self._last_by_fingerprint.get(key.request_fingerprint)
        if last is None:
            return 0.0
        return last + IDENTICAL_WINDOW_S - now

    def _burst_wait(self, key: RequestKey, now: float) -> float:
        dq = self._burst.get(key.burst_key)
        if dq is None or len(dq) < BURST_LIMIT:
            return 0.0
        return dq[len(dq) - BURST_LIMIT] + BURST_WINDOW_S - now

    def _budget_wait(self, key: RequestKey, now: float) -> float:
        total = sum(w for _, w in self._budget)
        if total + key.weight <= BUDGET_LIMIT:
            return 0.0
        excess = total + key.weight - BUDGET_LIMIT
        freed = 0
        for ts, w in self._budget:
            freed += w
            if freed >= excess:
                return ts + BUDGET_WINDOW_S - now
        return BUDGET_WINDOW_S  # unreachable while key.weight <= BUDGET_LIMIT

    def _record(self, key: RequestKey, now: float) -> None:
        self._last_by_fingerprint[key.request_fingerprint] = now
        self._burst.setdefault(key.burst_key, deque()).append(now)
        self._budget.append((now, key.weight))
