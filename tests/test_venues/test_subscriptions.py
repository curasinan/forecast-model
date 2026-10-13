"""Tests for the IBKR market-data subscription-line manager (fake clock, no network)."""

from __future__ import annotations

import logging
import threading

import pytest

from src.venues.ibkr.subscriptions import (
    Decision,
    LineBudgetExceeded,
    SubscriptionManager,
)


class FakeTime:
    """Deterministic clock; sleep() advances it so acquire_slot() never spins."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0.0, "manager must never sleep a non-positive duration"
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def make_manager(
    max_lines: int = 3,
    rate: float | None = None,
) -> tuple[SubscriptionManager, FakeTime]:
    ft = FakeTime()
    mgr = SubscriptionManager(
        max_lines,
        subscribe_rate_per_sec=rate,
        clock=ft.clock,
        sleeper=ft.sleep,
    )
    return mgr, ft


def test_default_rate_is_half_the_line_count():
    mgr, _ = make_manager(max_lines=10)
    assert mgr.subscribe_rate_per_sec == pytest.approx(5.0)


def test_explicit_rate_overrides_default():
    mgr, _ = make_manager(max_lines=10, rate=3.0)
    assert mgr.subscribe_rate_per_sec == pytest.approx(3.0)


def test_invalid_max_lines_rejected():
    with pytest.raises(ValueError):
        SubscriptionManager(0)


def test_invalid_rate_rejected():
    with pytest.raises(ValueError):
        SubscriptionManager(4, subscribe_rate_per_sec=0.0)


def test_fills_up_to_capacity_with_free_lines():
    mgr, _ = make_manager(max_lines=3)
    for i in range(3):
        d = mgr.request(f"m{i}")
        assert d == Decision(action="subscribe", market_id=f"m{i}", evict=None)
    assert sorted(mgr.active_ids()) == ["m0", "m1", "m2"]
    assert mgr.free_lines() == 0


def test_free_lines_and_is_active_track_state():
    mgr, _ = make_manager(max_lines=3)
    assert mgr.free_lines() == 3
    mgr.request("a")
    assert mgr.free_lines() == 2
    assert mgr.is_active("a")
    assert not mgr.is_active("b")


def test_re_request_of_active_line_is_noop():
    mgr, _ = make_manager(max_lines=3)
    mgr.request("a")
    d = mgr.request("a")
    assert d == Decision(action="noop", market_id="a", evict=None)
    assert mgr.stats()["active"] == 1  # not double counted


def test_eviction_picks_lowest_priority():
    mgr, _ = make_manager(max_lines=3)
    mgr.request("low", priority=1)
    mgr.request("mid", priority=5)
    mgr.request("high", priority=9)
    d = mgr.request("new", priority=6)
    assert d.action == "subscribe"
    assert d.market_id == "new"
    assert d.evict == "low"  # priority 1 is the lowest below 6
    assert not mgr.is_active("low")
    assert mgr.is_active("new")
    assert mgr.free_lines() == 0  # net line count unchanged


def test_eviction_breaks_priority_ties_by_least_recently_touched():
    mgr, _ = make_manager(max_lines=3)
    mgr.request("a", priority=1)  # seq 1
    mgr.request("b", priority=1)  # seq 2
    mgr.request("c", priority=1)  # seq 3
    mgr.touch("a")  # a now most-recently touched -> b is the LRU
    d = mgr.request("new", priority=2)
    assert d.evict == "b"


def test_eviction_tie_without_touch_evicts_oldest():
    mgr, _ = make_manager(max_lines=3)
    mgr.request("a", priority=2)
    mgr.request("b", priority=2)
    mgr.request("c", priority=2)
    d = mgr.request("new", priority=5)
    assert d.evict == "a"  # all equal priority, "a" is oldest


def test_equal_priority_at_capacity_raises():
    mgr, _ = make_manager(max_lines=2)
    mgr.request("a", priority=5)
    mgr.request("b", priority=5)
    with pytest.raises(LineBudgetExceeded):
        mgr.request("c", priority=5)


def test_higher_priority_incumbents_block_lower_requester():
    mgr, _ = make_manager(max_lines=2)
    mgr.request("a", priority=9)
    mgr.request("b", priority=9)
    with pytest.raises(LineBudgetExceeded):
        mgr.request("c", priority=3)
    # nothing changed
    assert sorted(mgr.active_ids()) == ["a", "b"]
    assert not mgr.is_active("c")


def test_rejection_does_not_evict_or_add():
    mgr, _ = make_manager(max_lines=1)
    mgr.request("a", priority=5)
    with pytest.raises(LineBudgetExceeded):
        mgr.request("b", priority=5)
    assert mgr.active_ids() == ["a"]


def test_release_frees_a_line():
    mgr, _ = make_manager(max_lines=2)
    mgr.request("a", priority=5)
    mgr.request("b", priority=5)
    assert mgr.free_lines() == 0
    assert mgr.release("a") is True
    assert mgr.free_lines() == 1
    # the freed line can now be taken without eviction
    d = mgr.request("c", priority=1)
    assert d == Decision(action="subscribe", market_id="c", evict=None)


def test_release_unknown_returns_false():
    mgr, _ = make_manager(max_lines=2)
    assert mgr.release("nope") is False


def test_touch_unknown_returns_false():
    mgr, _ = make_manager(max_lines=2)
    assert mgr.touch("nope") is False


def test_first_slot_is_immediate():
    mgr, ft = make_manager(max_lines=4)  # rate 2/s
    mgr.acquire_slot()
    assert ft.sleeps == []


def test_slots_spaced_by_inverse_rate():
    mgr, ft = make_manager(max_lines=4)  # rate 2/s -> 0.5s spacing
    mgr.acquire_slot()
    mgr.acquire_slot()
    mgr.acquire_slot()
    assert ft.sleeps == [pytest.approx(0.5), pytest.approx(0.5)]
    assert ft.now == pytest.approx(1.0)


def test_slot_wait_accounts_for_elapsed_time():
    mgr, ft = make_manager(max_lines=4)  # rate 2/s
    mgr.acquire_slot()
    ft.advance(0.25)  # 0.25s * 2/s = 0.5 token refilled
    mgr.acquire_slot()
    # needed 0.5 more token at 2/s -> 0.25s wait
    assert ft.sleeps == [pytest.approx(0.25)]


def test_slot_free_after_full_interval():
    mgr, ft = make_manager(max_lines=4)  # rate 2/s
    mgr.acquire_slot()
    ft.advance(0.5)  # a full token regenerated
    mgr.acquire_slot()
    assert ft.sleeps == []


def test_explicit_rate_controls_spacing():
    mgr, ft = make_manager(max_lines=100, rate=4.0)  # 0.25s spacing
    mgr.acquire_slot()
    mgr.acquire_slot()
    assert ft.sleeps == [pytest.approx(0.25)]


def test_slot_block_logs_warning(caplog):
    mgr, _ = make_manager(max_lines=4)
    mgr.acquire_slot()
    with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.subscriptions"):
        mgr.acquire_slot()
    assert any("subscription pacing" in r.getMessage() for r in caplog.records)


def test_eviction_logs_warning(caplog):
    mgr, _ = make_manager(max_lines=1)
    mgr.request("a", priority=1)
    with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.subscriptions"):
        mgr.request("b", priority=2)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("evicting" in m and "'a'" in m for m in msgs)


def test_rejection_logs_warning(caplog):
    mgr, _ = make_manager(max_lines=1)
    mgr.request("a", priority=5)
    with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.subscriptions"):
        with pytest.raises(LineBudgetExceeded):
            mgr.request("b", priority=5)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("rejecting" in m for m in msgs)


def test_stats_counters():
    mgr, _ = make_manager(max_lines=2)
    assert mgr.stats() == {"active": 0, "capacity": 2, "evictions": 0, "rejections": 0}

    mgr.request("a", priority=1)
    mgr.request("b", priority=1)
    assert mgr.stats() == {"active": 2, "capacity": 2, "evictions": 0, "rejections": 0}

    mgr.request("c", priority=5)  # evicts one
    assert mgr.stats()["evictions"] == 1
    assert mgr.stats()["active"] == 2

    with pytest.raises(LineBudgetExceeded):
        mgr.request("d", priority=0)  # nothing lower to evict
    assert mgr.stats()["rejections"] == 1


def test_thread_safety_smoke_unique_lines():
    mgr, _ = make_manager(max_lines=100, rate=1000.0)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(5):
                mgr.acquire_slot()
                mgr.request(f"t{tid}-{i}", priority=tid)
        except BaseException as exc:  # noqa: BLE001 - smoke test collects everything
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == []
    assert mgr.stats()["active"] == 40
