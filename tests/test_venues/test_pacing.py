"""Tests for the IBKR historical-request pacing limiter (fake clock, no real sleeping)."""

from __future__ import annotations

import logging
import threading

import pytest

from src.venues.ibkr.pacing import (
    BUDGET_LIMIT,
    BURST_LIMIT,
    HistoricalPacer,
    RequestKey,
)


class FakeTime:
    """Deterministic clock; sleep() advances it so acquire() never spins."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def clock(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0.0, "pacer must never sleep a non-positive duration"
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def make_pacer() -> tuple[HistoricalPacer, FakeTime]:
    ft = FakeTime()
    return HistoricalPacer(clock=ft.clock, sleeper=ft.sleep), ft


def key(
    fp: str = "fp",
    contract: str = "C1",
    exchange: str = "FORECASTX",
    tick: str = "TRADES",
) -> RequestKey:
    return RequestKey(
        contract_key=contract, exchange=exchange, tick_type=tick, request_fingerprint=fp
    )


def test_first_request_is_immediate():
    pacer, ft = make_pacer()
    pacer.acquire(key())
    assert ft.sleeps == []


def test_identical_request_blocks_full_15s():
    pacer, ft = make_pacer()
    pacer.acquire(key(fp="same"))
    pacer.acquire(key(fp="same"))
    assert ft.sleeps == [pytest.approx(15.0)]
    assert ft.now == pytest.approx(15.0)


def test_identical_request_wait_accounts_for_elapsed_time():
    pacer, ft = make_pacer()
    pacer.acquire(key(fp="same"))
    ft.advance(5.0)
    pacer.acquire(key(fp="same"))
    assert ft.sleeps == [pytest.approx(10.0)]


def test_identical_request_free_after_window():
    pacer, ft = make_pacer()
    pacer.acquire(key(fp="same"))
    ft.advance(15.0)
    pacer.acquire(key(fp="same"))
    assert ft.sleeps == []


def test_different_fingerprints_do_not_block():
    pacer, ft = make_pacer()
    pacer.acquire(key(fp="a"))
    pacer.acquire(key(fp="b"))
    assert ft.sleeps == []


def test_burst_limit_six_immediate_seventh_blocks():
    pacer, ft = make_pacer()
    for i in range(BURST_LIMIT):
        pacer.acquire(key(fp=f"fp{i}"))
    assert ft.sleeps == []
    pacer.acquire(key(fp="fp-extra"))
    assert ft.sleeps == [pytest.approx(2.0)]


def test_burst_wait_is_until_oldest_of_last_six_expires():
    pacer, ft = make_pacer()
    for i in range(3):
        pacer.acquire(key(fp=f"a{i}"))
    ft.advance(1.0)
    for i in range(3):
        pacer.acquire(key(fp=f"b{i}"))
    assert ft.sleeps == []
    pacer.acquire(key(fp="c"))  # oldest of the six is at t=0 -> free at t=2
    assert ft.sleeps == [pytest.approx(1.0)]


def test_burst_buckets_are_independent():
    pacer, ft = make_pacer()
    for i in range(BURST_LIMIT):
        pacer.acquire(key(fp=f"fp{i}", contract="A"))
    pacer.acquire(key(fp="other-contract", contract="B"))
    pacer.acquire(key(fp="other-tick", contract="A", tick="BID_ASK"))
    pacer.acquire(key(fp="other-exchange", contract="A", exchange="SMART"))
    assert ft.sleeps == []


def test_budget_sixty_immediate_sixty_first_blocks():
    pacer, ft = make_pacer()
    for i in range(BUDGET_LIMIT):
        pacer.acquire(key(fp=f"fp{i}", contract=f"C{i}"))
    assert ft.sleeps == []
    pacer.acquire(key(fp="extra", contract="Cx"))
    assert ft.sleeps == [pytest.approx(600.0)]


def test_bid_ask_double_counts_thirty_one_trips_budget():
    pacer, ft = make_pacer()
    for i in range(30):  # 30 * 2 = 60, exactly the budget
        pacer.acquire(key(fp=f"fp{i}", contract=f"C{i}", tick="BID_ASK"))
    assert ft.sleeps == []
    pacer.acquire(key(fp="fp30", contract="C30", tick="BID_ASK"))
    assert ft.sleeps == [pytest.approx(600.0)]


def test_bid_ask_double_counts_when_mixed():
    pacer, ft = make_pacer()
    for i in range(59):
        pacer.acquire(key(fp=f"fp{i}", contract=f"C{i}"))
    assert ft.sleeps == []
    # weight 2 would take the window to 61 > 60
    pacer.acquire(key(fp="ba", contract="Cba", tick="BID_ASK"))
    assert ft.sleeps == [pytest.approx(600.0)]


def test_budget_frees_as_window_slides():
    pacer, ft = make_pacer()
    for i in range(BUDGET_LIMIT):
        pacer.acquire(key(fp=f"fp{i}", contract=f"C{i}"))
    ft.advance(600.0)
    pacer.acquire(key(fp="later", contract="Cl"))
    assert ft.sleeps == []


def test_combined_rules_sleep_the_maximum_wait():
    pacer, ft = make_pacer()
    for i in range(BURST_LIMIT):
        pacer.acquire(key(fp=f"fp{i}"))
    pacer.acquire(key(fp="fp0"))
    assert ft.sleeps == [pytest.approx(15.0)]


def test_block_logs_warning_with_rule_and_wait(caplog):
    pacer, ft = make_pacer()
    pacer.acquire(key(fp="same"))
    with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.pacing"):
        pacer.acquire(key(fp="same"))
    messages = [r.getMessage() for r in caplog.records]
    assert any("identical-request" in m and "15.000" in m for m in messages)


def test_thread_safety_smoke_unique_keys():
    pacer, ft = make_pacer()
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(5):
                pacer.acquire(key(fp=f"t{tid}-{i}", contract=f"T{tid}-{i}"))
        except BaseException as exc:  # noqa: BLE001 - smoke test collects everything
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == []
    assert ft.sleeps == []  # 40 unique requests, no rule binds


def test_threaded_identical_requests_serialize():
    pacer, ft = make_pacer()
    done: list[str] = []

    def worker(name: str) -> None:
        pacer.acquire(key(fp="shared"))
        done.append(name)

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert sorted(done) == ["a", "b"]
    assert sum(ft.sleeps) == pytest.approx(15.0)
