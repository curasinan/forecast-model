"""GatewaySession unit tests: active heartbeat, backoff bounds, hooks,
event sinks, expected-window classification, JSONL sink. No gateway, no
network, no ib_async import: the connection is a FakeIB behind a fake
connector, and time is a fake clock advanced by a fake sleeper.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import partial

import pytest

from src.venues.ibkr.clients import ClientIdInUseError
from src.venues.ibkr.gateway import (
    BACKOFF_CAP_S,
    ConnectionEvent,
    GatewaySession,
    append_event_jsonl,
    in_restart_window,
    parse_restart_window,
)
from src.venues.ibkr.settings import IBKRSettings

NOON = datetime(2026, 8, 17, 12, 0, 0)  # outside the default restart window


def _settings(window: str = "06:45-08:15") -> IBKRSettings:
    return IBKRSettings(
        _env_file=None, port=4002, trading_mode="paper", restart_window_local=window
    )


class FakeIB:
    """Controllable stand-in for ib_async.IB (no ib_async import)."""

    def __init__(self):
        self.connected = True
        self.heartbeat_ok = True
        self.heartbeat_calls = 0
        self.disconnect_calls = 0

    def isConnected(self) -> bool:
        return self.connected

    def reqCurrentTime(self):
        self.heartbeat_calls += 1
        if not self.heartbeat_ok:
            raise TimeoutError("gateway did not answer reqCurrentTime")
        return 1755400000

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


class FakeConnector:
    """Yields queued outcomes (Exception -> raised, FakeIB -> returned)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, role, settings):
        self.calls.append(role)
        if not self.outcomes:  # never loop forever if a test miscounts
            return FakeIB()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSleeper:
    """Records requested sleeps and advances the fake clock by them."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds


class FakeLocalNow:
    """Local wall clock derived from the fake monotonic clock."""

    def __init__(self, start: datetime, clock: FakeClock):
        self.start = start
        self.clock = clock
        self.origin = clock.now

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=self.clock.now - self.origin)


def make_session(connector, window: str = "06:45-08:15", local_start: datetime = NOON,
                 validator=None):
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    session = GatewaySession(
        _settings(window),
        "data_collector",
        connect_validator=validator,
        connect=connector,
        sleep=sleeper,
        clock=clock,
        local_now=FakeLocalNow(local_start, clock),
    )
    return session, clock, sleeper


def _local(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 17, hour, minute)


class TestWindowParsing:
    def test_parse_basic(self):
        assert parse_restart_window("06:45-08:15") == (405, 495)

    def test_parse_strips_whitespace(self):
        assert parse_restart_window(" 23:30-00:45 ") == (1410, 45)

    @pytest.mark.parametrize("bad", [
        "", "06:45", "0645-0815", "06:45-25:00", "06:60-07:00",
        "06:45-07:00-08:00", "24:00-01:00", "aa:bb-cc:dd",
    ])
    def test_parse_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_restart_window(bad)

    def test_boundary_minutes_inclusive(self):
        window = parse_restart_window("06:45-08:15")
        assert in_restart_window(_local(6, 45), window)
        assert in_restart_window(_local(8, 15), window)
        assert not in_restart_window(_local(6, 44), window)
        assert not in_restart_window(_local(8, 16), window)
        assert in_restart_window(_local(7, 30), window)

    def test_midnight_crossing_window(self):
        window = parse_restart_window("23:30-00:45")
        assert in_restart_window(_local(23, 30), window)
        assert in_restart_window(_local(0, 0), window)
        assert in_restart_window(_local(0, 45), window)
        assert not in_restart_window(_local(23, 29), window)
        assert not in_restart_window(_local(0, 46), window)
        assert not in_restart_window(_local(12, 0), window)

    def test_bad_window_fails_at_construction(self):
        with pytest.raises(ValueError):
            make_session(FakeConnector(FakeIB()), window="nonsense")


class TestEnsureConnected:
    def test_clean_initial_connect_emits_nothing_runs_no_hooks(self):
        connector = FakeConnector(ib := FakeIB())
        session, _, _ = make_session(connector)
        events: list[ConnectionEvent] = []
        hook_runs: list[int] = []
        session.on_event(events.append)
        session.on_reconnect(lambda: hook_runs.append(1))

        assert session.ensure_connected() is ib
        assert events == []
        assert hook_runs == []
        assert connector.calls == ["data_collector"]

    def test_healthy_session_is_reused_and_probed(self):
        connector = FakeConnector(ib := FakeIB())
        session, _, _ = make_session(connector)
        assert session.ensure_connected() is ib
        assert session.ensure_connected() is ib
        assert len(connector.calls) == 1
        assert ib.heartbeat_calls == 1  # probed on the second call (fast path first)

    def test_heartbeat_failure_triggers_reconnect(self):
        connector = FakeConnector(ib1 := FakeIB(), ib2 := FakeIB())
        session, _, _ = make_session(connector)
        events: list[ConnectionEvent] = []
        session.on_event(events.append)

        assert session.ensure_connected() is ib1
        ib1.heartbeat_ok = False  # socket still "connected": TCP-alive is not enough
        assert session.ensure_connected() is ib2
        assert ib1.heartbeat_calls == 1
        assert ib1.disconnect_calls == 1
        assert [e.kind for e in events] == ["heartbeat_miss", "disconnect", "reconnect"]

    def test_socket_down_triggers_reconnect_without_heartbeat_miss(self):
        connector = FakeConnector(ib1 := FakeIB(), ib2 := FakeIB())
        session, _, _ = make_session(connector)
        events: list[ConnectionEvent] = []
        session.on_event(events.append)

        session.ensure_connected()
        ib1.connected = False
        assert session.ensure_connected() is ib2
        assert [e.kind for e in events] == ["disconnect", "reconnect"]

    def test_validator_errors_propagate_not_retried(self):
        def validator(ib):
            raise RuntimeError("mode mismatch")

        connector = FakeConnector(FakeIB(), FakeIB())
        session, _, _ = make_session(connector, validator=validator)
        with pytest.raises(RuntimeError, match="mode mismatch"):
            session.ensure_connected()
        assert len(connector.calls) == 1  # config error killed the loop, no retry

    def test_client_id_in_use_error_propagates_not_retried(self):
        # A clientId collision means a second process already owns this
        # role -- a config error, not a transient outage -- so it must
        # kill the process instead of being retried forever (audit M5).
        connector = FakeConnector(ClientIdInUseError("client id 2 in use"), FakeIB())
        session, _, sleeper = make_session(connector)
        with pytest.raises(ClientIdInUseError, match="client id 2 in use"):
            session.ensure_connected()
        assert len(connector.calls) == 1  # config error killed the loop, no retry
        assert sleeper.sleeps == []  # never entered backoff


class TestBackoff:
    def test_backoff_doubles_to_cap_with_jitter_bounds(self):
        failures = [ConnectionRefusedError("refused")] * 8
        connector = FakeConnector(*failures, FakeIB())
        session, _, sleeper = make_session(connector)
        session.ensure_connected()

        expected_bases = [1, 2, 4, 8, 16, 32, 60, 60]
        assert len(sleeper.sleeps) == len(expected_bases)
        for actual, base in zip(sleeper.sleeps, expected_bases):
            assert 0.8 * base <= actual <= 1.2 * base
        assert max(sleeper.sleeps) <= 1.2 * BACKOFF_CAP_S


class TestReconnectHooks:
    def test_hooks_run_on_every_reconnect_and_errors_swallowed(self):
        connector = FakeConnector(ib1 := FakeIB(), ib2 := FakeIB(), ib3 := FakeIB())
        session, _, _ = make_session(connector)
        runs: list[str] = []
        session.on_reconnect(lambda: runs.append("a"))

        def bad_hook():
            runs.append("bad")
            raise RuntimeError("resubscribe blew up")

        session.on_reconnect(bad_hook)
        session.on_reconnect(lambda: runs.append("b"))

        assert session.ensure_connected() is ib1
        assert runs == []  # nothing to re-establish on the first-ever connect

        ib1.heartbeat_ok = False
        assert session.ensure_connected() is ib2
        assert runs == ["a", "bad", "b"]  # bad_hook's error did not stop the rest

        ib2.connected = False
        assert session.ensure_connected() is ib3
        assert runs == ["a", "bad", "b", "a", "bad", "b"]


class TestEvents:
    def test_downtime_seconds_from_fake_clock(self):
        connector = FakeConnector(
            ib1 := FakeIB(),
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
            FakeIB(),
        )
        session, _, sleeper = make_session(connector)
        events: list[ConnectionEvent] = []
        session.on_event(events.append)

        session.ensure_connected()
        ib1.heartbeat_ok = False
        session.ensure_connected()

        by_kind = {e.kind: e for e in events}
        assert by_kind["heartbeat_miss"].downtime_seconds is None
        assert by_kind["disconnect"].downtime_seconds is None
        # The outage clock starts at the disconnect; only the two backoff
        # sleeps advanced the fake clock before the reconnect landed.
        assert by_kind["reconnect"].downtime_seconds == pytest.approx(
            sum(sleeper.sleeps), abs=0.01
        )
        assert by_kind["reconnect"].detail == "connected after 3 attempt(s)"
        for event in events:
            assert event.expected is False  # noon is outside 06:45-08:15
            assert event.ts_utc  # ISO stamp present

    def test_sink_exceptions_are_swallowed(self):
        connector = FakeConnector(ib1 := FakeIB(), FakeIB())
        session, _, _ = make_session(connector)
        received: list[ConnectionEvent] = []

        def bad_sink(event):
            raise RuntimeError("sink exploded")

        session.on_event(bad_sink)
        session.on_event(received.append)

        session.ensure_connected()
        ib1.connected = False
        session.ensure_connected()  # must not raise despite bad_sink
        assert [e.kind for e in received] == ["disconnect", "reconnect"]

    def test_events_inside_window_are_expected(self):
        connector = FakeConnector(ib1 := FakeIB(), FakeIB())
        session, _, _ = make_session(connector, local_start=_local(7, 0))
        events: list[ConnectionEvent] = []
        session.on_event(events.append)

        session.ensure_connected()
        ib1.heartbeat_ok = False
        session.ensure_connected()
        assert len(events) == 3
        assert all(e.expected for e in events)

    def test_gave_up_window_emitted_once_when_outage_outlives_window(self):
        # Outage starts at 08:14 inside 06:45-08:15; nine failed attempts
        # accumulate >= 146s of backoff sleep even at minimum jitter, so the
        # local clock is guaranteed past 08:16 while still down.
        failures = [ConnectionRefusedError("refused")] * 9
        connector = FakeConnector(ib1 := FakeIB(), *failures, FakeIB())
        session, _, _ = make_session(connector, local_start=_local(8, 14))
        events: list[ConnectionEvent] = []
        session.on_event(events.append)

        session.ensure_connected()
        ib1.heartbeat_ok = False
        session.ensure_connected()

        kinds = [e.kind for e in events]
        assert kinds.count("gave_up_window") == 1
        assert kinds.index("gave_up_window") < kinds.index("reconnect")
        gave_up = next(e for e in events if e.kind == "gave_up_window")
        assert gave_up.expected is False  # by construction outside the window
        assert gave_up.downtime_seconds is not None
        assert gave_up.downtime_seconds > 0
        assert events[0].expected is True  # the disconnect itself was expected
        assert events[-1].kind == "reconnect"


class TestJsonlSink:
    def _event(self, kind: str = "disconnect", downtime=None) -> ConnectionEvent:
        return ConnectionEvent(kind=kind, ts_utc="2026-08-17T04:01:00+00:00",
                               detail="test detail", expected=True,
                               downtime_seconds=downtime)

    def test_appends_valid_json_lines_and_creates_parents(self, tmp_path):
        path = tmp_path / "logs" / "events.jsonl"
        append_event_jsonl(self._event("disconnect"), path)
        append_event_jsonl(self._event("reconnect", downtime=12.5), path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first == {
            "kind": "disconnect",
            "ts_utc": "2026-08-17T04:01:00+00:00",
            "detail": "test detail",
            "expected": True,
            "downtime_seconds": None,
        }
        assert second["kind"] == "reconnect"
        assert second["downtime_seconds"] == 12.5

    def test_registered_as_session_sink(self, tmp_path):
        path = tmp_path / "events.jsonl"
        connector = FakeConnector(ib1 := FakeIB(), FakeIB())
        session, _, _ = make_session(connector)
        session.on_event(partial(append_event_jsonl, path=path))

        session.ensure_connected()
        ib1.heartbeat_ok = False
        session.ensure_connected()

        records = [json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines()]
        assert [r["kind"] for r in records] == ["heartbeat_miss", "disconnect", "reconnect"]
        assert records[-1]["downtime_seconds"] is not None
