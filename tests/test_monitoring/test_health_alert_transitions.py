"""Alert on health state changes, not on every unhealthy run.

The probe (scripts/check_ibkr_recorder.py) runs hourly. It used to alert on
every non-OK run -- about 15 identical DOWN messages a day during the
2026-09-29..10-03 outage, until the operator stopped reading them. It now
alerts when the (recorder_status, sync_status) pair changes, reminds once per
24 hours of an unchanged outage, retries an undelivered alert on its next run,
and logs every send attempt durably. Layers tested here, bottom up:

  1. decide_alert: a pure function of (previous state, current pair, now), so
     the decision matrix and the exact 24 h boundary need no patched clock
  2. the alert state file: atomic writes; a corrupt file fails toward alerting
  3. main(): wiring, durable log lines, exit codes, channel independence
  4. --test-alert
  5. secret hygiene: a leaky exception must not put the bot token anywhere

Everything runs against tmp_path. send_alert is replaced by a recorder, or
requests.post is patched, in every test that gets near the alert path (the
conftest.py next to this file also trips on any unpatched requests.post).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import monitoring.heartbeat as heartbeat_mod
import scripts.check_ibkr_recorder as probe

T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
OK_PAIR = ("OK", "OK")


def _prev(pair=("DOWN", "OK"), **overrides) -> probe.AlertState:
    """A stored alert state: an outage three days old whose last alert (a
    delivered one) went out an hour ago. Override any field."""
    fields = {
        "since": T0 - timedelta(days=3),
        "last_alert": T0 - timedelta(hours=1),
        "last_alert_ok": True,
        "outage_since": None if pair == OK_PAIR else T0 - timedelta(days=3),
    }
    fields.update(overrides)
    return probe.AlertState(pair[0], pair[1], **fields)


# --------------------------------------------------------------------------- #
# 1. decide_alert: when to send
# --------------------------------------------------------------------------- #


def test_no_state_reads_as_healthy_with_nothing_alerted_yet():
    state = probe.NO_ALERT_STATE
    assert state.pair == OK_PAIR
    assert state.last_alert is None
    assert state.last_alert_ok is None
    assert state.outage_since is None


def test_unhealthy_run_with_no_state_is_a_transition_from_ok():
    decision = probe.decide_alert(probe.NO_ALERT_STATE, ("DOWN", "OK"), T0)

    assert decision.kind == "transition"
    assert decision.prefix == "[OK -> DOWN]"
    assert decision.state.pair == ("DOWN", "OK")
    assert decision.state.since == T0
    assert decision.state.outage_since == T0
    # Nothing has been sent for the new pair yet: record_alert_attempt fills this in.
    assert decision.state.last_alert is None
    assert decision.state.last_alert_ok is None


def test_recovery_is_a_transition_that_reports_how_long_the_outage_lasted():
    prev = _prev(("DOWN", "OK"), outage_since=T0 - timedelta(days=4, hours=12, minutes=5))

    decision = probe.decide_alert(prev, OK_PAIR, T0)

    assert decision.kind == "transition"
    assert decision.prefix == "[DOWN -> OK] recovered after 4d 12h:"
    assert decision.state.pair == OK_PAIR
    assert decision.state.since == T0
    assert decision.state.outage_since is None


@pytest.mark.parametrize(
    ("before", "after", "prefix"),
    [
        (("DOWN", "SYNC_STALE"), ("OK", "SYNC_STALE"), "[DOWN+SYNC_STALE -> SYNC_STALE]"),
        (("DOWN", "OK"), ("DOWN", "SYNC_STALE"), "[DOWN -> DOWN+SYNC_STALE]"),
        (("STALE", "OK"), ("DOWN", "OK"), "[STALE -> DOWN]"),
        (("OK", "SYNC_STALE"), ("STALE", "OK"), "[SYNC_STALE -> STALE]"),
    ],
)
def test_change_of_kind_is_a_transition_and_the_outage_clock_keeps_running(before, after, prefix):
    outage_start = T0 - timedelta(days=2, hours=3)
    # The previous alert was only an hour ago and was delivered: a changed pair
    # is news whatever the 24 h rule says.
    prev = _prev(before, outage_since=outage_start)

    decision = probe.decide_alert(prev, after, T0)

    assert decision.kind == "transition"
    assert decision.prefix == prefix
    assert decision.state.pair == after
    assert decision.state.since == T0  # the pair changed now ...
    assert decision.state.outage_since == outage_start  # ... the outage did not restart


@pytest.mark.parametrize(
    "prev",
    [
        probe.NO_ALERT_STATE,
        _prev(OK_PAIR, last_alert=T0 - timedelta(days=30)),
        _prev(OK_PAIR, last_alert=None, last_alert_ok=None),
        # A recovery alert that failed to send is NOT retried: the retry rule
        # applies only while the state is not all-OK.
        _prev(OK_PAIR, last_alert_ok=False),
    ],
    ids=["no-state", "healthy-for-30-days", "healthy-never-alerted", "healthy-failed-recovery"],
)
def test_unchanged_all_ok_never_sends(prev):
    decision = probe.decide_alert(prev, OK_PAIR, T0)

    assert decision.kind is None
    assert decision.prefix == ""
    assert decision.state == prev


def test_reminder_interval_is_24_hours():
    assert probe.ALERT_REMINDER_INTERVAL == timedelta(hours=24)


@pytest.mark.parametrize(
    ("age", "expected_kind"),
    [
        (timedelta(hours=23, minutes=59, seconds=59), None),
        (timedelta(hours=24), "reminder"),
        (timedelta(hours=24, seconds=1), "reminder"),
    ],
    ids=["23:59:59", "24:00:00", "24:00:01"],
)
def test_reminder_boundary_is_exactly_24_hours_after_the_last_successful_alert(age, expected_kind):
    prev = _prev(("DOWN", "OK"), last_alert=T0 - age, last_alert_ok=True)

    assert probe.decide_alert(prev, ("DOWN", "OK"), T0).kind == expected_kind


def test_reminder_names_the_outage_length_and_changes_no_bookkeeping():
    started = T0 - timedelta(days=2, hours=3)
    prev = _prev(
        ("DOWN", "OK"), since=started, outage_since=started, last_alert=T0 - timedelta(hours=24)
    )

    decision = probe.decide_alert(prev, ("DOWN", "OK"), T0)

    assert decision.kind == "reminder"
    assert decision.prefix == "[still DOWN, 2d 3h]"
    # Only the alert time changes, and record_alert_attempt does that.
    assert decision.state == prev


def test_reminder_clock_runs_from_the_last_alert_not_from_the_start_of_the_outage():
    started = T0 - timedelta(days=9)
    prev = _prev(
        ("DOWN", "OK"), since=started, outage_since=started, last_alert=T0 - timedelta(hours=1)
    )

    assert probe.decide_alert(prev, ("DOWN", "OK"), T0).kind is None


@pytest.mark.parametrize(
    "age",
    [
        timedelta(seconds=1),
        timedelta(minutes=59),
        timedelta(hours=23, minutes=59, seconds=59),
        timedelta(hours=24),
        timedelta(days=5),
    ],
    ids=["1s", "59m", "23:59:59", "24h", "5d"],
)
def test_undelivered_previous_attempt_is_retried_whatever_its_age(age):
    started = T0 - timedelta(hours=7, minutes=20)
    prev = _prev(
        ("DOWN", "OK"),
        since=started,
        outage_since=started,
        last_alert=T0 - age,
        last_alert_ok=False,
    )

    decision = probe.decide_alert(prev, ("DOWN", "OK"), T0)

    assert decision.kind == "retry"
    assert decision.prefix == "[still DOWN, 7h 20m]"
    assert decision.state == prev


@pytest.mark.parametrize(
    ("last_alert", "last_alert_ok"),
    [
        (None, True),
        (None, None),
        (None, False),
        (T0 - timedelta(hours=1), None),
    ],
    ids=["ok-without-time", "nothing-recorded", "failed-without-time", "no-outcome"],
)
def test_unhealthy_pair_with_no_confirmed_alert_is_retried(last_alert, last_alert_ok):
    """Nothing proves the operator was ever told (a hand-edited or partial
    state file), so the probe tells them: it fails toward alerting."""
    prev = _prev(("DOWN", "OK"), last_alert=last_alert, last_alert_ok=last_alert_ok)

    assert probe.decide_alert(prev, ("DOWN", "OK"), T0).kind == "retry"


def test_unhealthy_previous_state_without_an_outage_start_is_tolerated():
    prev = replace(_prev(("DOWN", "OK")), outage_since=None)

    recovery = probe.decide_alert(prev, OK_PAIR, T0)
    retry = probe.decide_alert(replace(prev, last_alert_ok=False), ("DOWN", "OK"), T0)

    assert recovery.kind == "transition"
    assert recovery.prefix == "[DOWN -> OK] recovered after 0m:"
    assert retry.kind == "retry"
    assert retry.state.outage_since is None  # unchanged: nothing was invented


@pytest.mark.parametrize("delivered", [True, False])
def test_record_alert_attempt_stamps_the_attempt_and_its_outcome(delivered):
    prev = _prev(("DOWN", "OK"), last_alert=T0 - timedelta(days=1), last_alert_ok=not delivered)

    after = probe.record_alert_attempt(prev, T0, delivered)

    assert after.last_alert == T0
    assert after.last_alert_ok is delivered
    # Nothing else moved.
    assert replace(after, last_alert=prev.last_alert, last_alert_ok=prev.last_alert_ok) == prev


# --------------------------------------------------------------------------- #
# Message prefix: one short ASCII fragment saying what happened
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pair", "label"),
    [
        (("OK", "OK"), "OK"),
        (("DOWN", "OK"), "DOWN"),
        (("OK", "SYNC_STALE"), "SYNC_STALE"),
        (("DOWN", "SYNC_STALE"), "DOWN+SYNC_STALE"),
    ],
)
def test_pair_label_names_every_failing_axis(pair, label):
    assert probe._pair_label(pair) == label


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=-30), "0m"),
        (timedelta(0), "0m"),
        (timedelta(seconds=59), "0m"),
        (timedelta(seconds=60), "1m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h 0m"),
        (timedelta(hours=3, minutes=20), "3h 20m"),
        (timedelta(hours=23, minutes=59, seconds=59), "23h 59m"),
        (timedelta(hours=24), "1d 0h"),
        (timedelta(days=2, hours=3, minutes=59), "2d 3h"),
        (timedelta(days=4, hours=12, minutes=5), "4d 12h"),
    ],
)
def test_format_duration_keeps_two_units(delta, text):
    assert probe._format_duration(delta) == text


def test_every_prefix_is_one_ascii_line():
    prefixes = [
        probe.decide_alert(probe.NO_ALERT_STATE, ("DOWN", "SYNC_STALE"), T0).prefix,
        probe.decide_alert(_prev(("DOWN", "SYNC_STALE")), OK_PAIR, T0).prefix,
        probe.decide_alert(_prev(("DOWN", "OK")), ("STALE", "OK"), T0).prefix,
        probe.decide_alert(
            _prev(("DOWN", "OK"), last_alert_ok=False), ("DOWN", "OK"), T0
        ).prefix,
        probe.decide_alert(
            _prev(("DOWN", "OK"), last_alert=T0 - timedelta(days=2)), ("DOWN", "OK"), T0
        ).prefix,
    ]
    assert all(prefix and prefix.isascii() and "\n" not in prefix for prefix in prefixes)


# --------------------------------------------------------------------------- #
# 2. The alert state file
# --------------------------------------------------------------------------- #

FAKE_TOKEN = "123456789:FAKE-test-token-NOT-A-REAL-SECRET"
FAKE_CHAT_ID = "-1000000000000"


def _on_disk(pair=("DOWN", "OK"), *, now=T0, **changes) -> dict:
    """The documented on-disk form of ``_prev(pair)`` as of ``now``, written
    out by hand so a renamed key shows up here rather than being masked by a
    round trip."""
    payload = {
        "recorder_status": pair[0],
        "sync_status": pair[1],
        "since_utc": (now - timedelta(days=3)).isoformat(),
        "last_alert_utc": (now - timedelta(hours=1)).isoformat(),
        "last_alert_ok": True,
        "outage_since_utc": None if pair == OK_PAIR else (now - timedelta(days=3)).isoformat(),
    }
    payload.update(changes)
    return payload


def test_documented_on_disk_form_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_on_disk(("DOWN", "SYNC_STALE"), last_alert_ok=False)), "utf-8")

    assert probe.load_alert_state(path) == _prev(("DOWN", "SYNC_STALE"), last_alert_ok=False)


@pytest.mark.parametrize(
    "state",
    [
        _prev(("DOWN", "SYNC_STALE"), last_alert_ok=False),
        _prev(("OK", "SYNC_STALE")),
        _prev(OK_PAIR),
        probe.AlertState(since=T0),  # healthy, never alerted: three nulls
    ],
    ids=["down+sync", "sync-only", "healthy-after-recovery", "healthy-never-alerted"],
)
def test_saved_state_loads_back_identically(tmp_path, state):
    path = tmp_path / "state.json"

    probe.save_alert_state(path, state)

    assert probe.load_alert_state(path) == state


def test_state_file_holds_only_the_documented_keys_and_no_environment_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALERT_METHOD", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    path = tmp_path / "state.json"

    probe.save_alert_state(path, _prev(("DOWN", "OK")))

    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == _on_disk(("DOWN", "OK"))
    for secret in (FAKE_TOKEN, FAKE_TOKEN.split(":", 1)[1], FAKE_CHAT_ID, "telegram"):
        assert secret not in text


def test_healthy_state_is_stored_with_nulls_not_invented_values(tmp_path):
    path = tmp_path / "state.json"

    probe.save_alert_state(path, probe.AlertState(since=T0))

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "recorder_status": "OK",
        "sync_status": "OK",
        "since_utc": T0.isoformat(),
        "last_alert_utc": None,
        "last_alert_ok": None,
        "outage_since_utc": None,
    }


def test_missing_state_file_reads_as_no_state(tmp_path):
    assert probe.load_alert_state(tmp_path / "nope.json") == probe.NO_ALERT_STATE


_VALID_JSON = json.dumps(_on_disk(("DOWN", "OK")))
_CORRUPT_CONTENTS = {
    "empty": b"",
    "not-json": b"{not json",
    "truncated": _VALID_JSON[:40].encode(),
    "binary-garbage": b"\xff\xfe\x00garbage\x80",
    "json-null": b"null",
    "json-list": b"[]",
    "json-string": b'"DOWN"',
    "absurdly-nested": b"[" * 200_000,
    "status-missing": json.dumps(
        {k: v for k, v in _on_disk().items() if k != "sync_status"}
    ).encode(),
    "status-not-a-string": json.dumps(_on_disk(recorder_status=7)).encode(),
    "status-lowercase": json.dumps(_on_disk(recorder_status="down")).encode(),
    "status-empty": json.dumps(_on_disk(recorder_status="")).encode(),
    "status-too-long": json.dumps(_on_disk(recorder_status="A" * 33)).encode(),
    # A forged status must never reach the alert text or the durable log.
    "status-with-newline": json.dumps(_on_disk(recorder_status="DOWN\nforged")).encode(),
    "timestamp-garbage": json.dumps(_on_disk(since_utc="yesterday")).encode(),
    "timestamp-blank": json.dumps(_on_disk(last_alert_utc="  ")).encode(),
    "timestamp-not-a-string": json.dumps(_on_disk(outage_since_utc=12345)).encode(),
    "last-alert-ok-a-string": json.dumps(_on_disk(last_alert_ok="yes")).encode(),
    "last-alert-ok-an-int": json.dumps(_on_disk(last_alert_ok=1)).encode(),
    "unhealthy-without-outage-start": json.dumps(_on_disk(outage_since_utc=None)).encode(),
}


@pytest.mark.parametrize("content", list(_CORRUPT_CONTENTS.values()), ids=list(_CORRUPT_CONTENTS))
def test_corrupt_state_file_reads_as_no_state_and_never_raises(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_bytes(content)

    assert probe.load_alert_state(path) == probe.NO_ALERT_STATE


def test_unreadable_state_path_reads_as_no_state(tmp_path):
    """A directory where the file should be: open() raises an OSError."""
    path = tmp_path / "state.json"
    path.mkdir()

    assert probe.load_alert_state(path) == probe.NO_ALERT_STATE


def test_save_stages_a_complete_temp_file_next_to_the_target_then_replaces(
    tmp_path, monkeypatch
):
    path = tmp_path / "logs" / "state.json"
    path.parent.mkdir()
    path.write_text("OLD CONTENT", encoding="utf-8")
    seen = []
    real_replace = probe.os.replace

    def _spying_replace(src, dst):
        seen.append((
            Path(src),
            Path(dst),
            Path(src).read_text(encoding="utf-8"),
            path.read_text(encoding="utf-8"),
        ))
        real_replace(src, dst)

    monkeypatch.setattr(probe.os, "replace", _spying_replace)
    state = _prev(("DOWN", "OK"))

    probe.save_alert_state(path, state)

    ((staged_path, target, staged_text, visible_meanwhile),) = seen
    assert target == path
    assert staged_path != path
    assert staged_path.parent == path.parent  # same directory: the replace is atomic
    assert json.loads(staged_text) == _on_disk(("DOWN", "OK"))  # complete before it goes live
    assert visible_meanwhile == "OLD CONTENT"  # readers never see a half-written file
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind
    assert probe.load_alert_state(path) == state


def test_failed_replace_keeps_the_previous_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("OLD CONTENT", encoding="utf-8")

    def _locked(src, dst):
        raise PermissionError("file is locked (simulated)")

    monkeypatch.setattr(probe.os, "replace", _locked)

    with pytest.raises(PermissionError):
        probe.save_alert_state(path, _prev(("DOWN", "OK")))

    assert path.read_text(encoding="utf-8") == "OLD CONTENT"
    assert list(tmp_path.iterdir()) == [path]


def test_save_creates_the_missing_directory(tmp_path):
    path = tmp_path / "no" / "such" / "dir" / "state.json"
    state = _prev(("DOWN", "OK"))

    probe.save_alert_state(path, state)

    assert probe.load_alert_state(path) == state


# --------------------------------------------------------------------------- #
# 3. main(): wiring, durable log lines, exit codes, channel independence
# --------------------------------------------------------------------------- #

SENT_LINE = re.compile(
    r"^(?P<ts>\S+) ALERT_SENT (?P<kind>transition|reminder|retry|test) (?P<status>\S+)$"
)
FAILED_LINE = re.compile(
    r"^(?P<ts>\S+) ALERT_FAILED (?P<kind>transition|reminder|retry|test) "
    r"(?P<status>\S+) (?P<reason>\S+)$"
)


class _FakeSendAlert:
    """Stands in for monitoring.heartbeat.send_alert: records every message and
    answers with ``outcome`` -- True (delivered), False (not delivered), or an
    exception instance to raise."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.outcome: object = True

    def __call__(self, message, method=None):
        self.messages.append(message)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def sent(monkeypatch) -> _FakeSendAlert:
    fake = _FakeSendAlert()
    monkeypatch.setattr(probe, "send_alert", fake)
    return fake


class _Run:
    """The files of one probe run, all under tmp_path -- never the real logs/
    directory or the real home. ``make`` arranges the inputs so the next run
    sees the wanted (recorder, sync) pair."""

    def __init__(self, root: Path, clock: datetime | None = None) -> None:
        self.clock = clock
        self.heartbeat_path = root / "state" / "recorder_state.json"
        self.events_path = root / "ibkr_events.jsonl"
        self.sync_path = root / "paper" / "ibkr_sync_runs.jsonl"
        self.snapshot_path = root / "no_snapshot.json"
        self.health_log = root / "logs" / "ibkr_health.log"
        self.alert_state = root / "logs" / "ibkr_health_alert_state.json"

    def now(self) -> datetime:
        return self.clock or datetime.now(timezone.utc)

    def make(self, recorder: str = "OK", sync: str = "OK") -> _Run:
        now = self.now()
        heartbeat_age_min = {"OK": 5, "STALE": 120, "DOWN": 5}[recorder]
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(json.dumps({
            "ts": (now - timedelta(minutes=heartbeat_age_min)).isoformat(),
            "rows": 2900,
            "sweep_seconds": 780.0,
            "universe_size": 2900,
        }), encoding="utf-8")
        if recorder == "DOWN":
            self.events_path.write_text(json.dumps({
                "kind": "disconnect",
                "ts_utc": (now - timedelta(minutes=30)).isoformat(),
                "expected": False,
            }) + "\n", encoding="utf-8")
        elif self.events_path.exists():
            self.events_path.unlink()
        sync_age_hours = {"OK": 1.0, "SYNC_STALE": 27.0}[sync]
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_path.write_text(json.dumps({
            "ts_utc": (now - timedelta(hours=sync_age_hours)).isoformat(),
            "ok": True,
        }) + "\n", encoding="utf-8")
        return self

    def argv(self, *extra: str, with_alert_state: bool = True) -> list[str]:
        argv = [
            "--state-path", str(self.heartbeat_path),
            "--events-path", str(self.events_path),
            "--sync-runs-path", str(self.sync_path),
            "--snapshot-path", str(self.snapshot_path),
            "--health-log-path", str(self.health_log),
        ]
        if with_alert_state:
            argv += ["--alert-state-path", str(self.alert_state)]
        return argv + list(extra)

    def run(self, *extra: str) -> int:
        return probe.main(self.argv(*extra))

    def store(self, pair=("DOWN", "OK"), **changes) -> None:
        """Pre-seed the alert state file as of this run's clock."""
        self.alert_state.parent.mkdir(parents=True, exist_ok=True)
        payload = _on_disk(pair, now=self.now(), **changes)
        self.alert_state.write_text(json.dumps(payload), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.alert_state.read_text(encoding="utf-8"))

    def log_lines(self) -> list[str]:
        if not self.health_log.exists():
            return []
        return self.health_log.read_text(encoding="utf-8").splitlines()

    def alert_lines(self) -> list[str]:
        return [line for line in self.log_lines() if " ALERT_" in line]

    def status_lines(self) -> list[str]:
        return [line for line in self.log_lines() if " ALERT_" not in line]


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _freeze_clock(monkeypatch, instant: datetime):
    """Make the probe's ``datetime.now`` tick one second per call from
    ``instant``, so a second reading of the clock within a run is visible."""

    class _Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            _Clock.calls += 1
            return instant + timedelta(seconds=_Clock.calls - 1)

    monkeypatch.setattr(probe, "datetime", _Clock)
    return _Clock


# -- when to send --------------------------------------------------------------


def test_first_unhealthy_run_sends_one_transition_alert_and_stores_the_state(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")

    rc = run.run("--quiet")

    assert rc == 1
    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[OK -> STALE] ibkr recorder STALE: heartbeat is 120 min")
    stored = run.stored()
    assert (stored["recorder_status"], stored["sync_status"]) == ("STALE", "OK")
    assert stored["last_alert_ok"] is True
    # One run, one clock: the pair changed, was alerted and began its outage at one instant.
    assert stored["since_utc"] == stored["last_alert_utc"] == stored["outage_since_utc"]


def test_unchanged_outage_stays_silent_but_keeps_its_status_line_per_run(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")
    assert run.run("--quiet") == 1

    assert run.run("--quiet") == 1
    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1  # only the transition
    assert len(run.status_lines()) == 3  # the durable log keeps one line per unhealthy run
    assert len(run.alert_lines()) == 1


def test_silent_unchanged_run_does_not_rewrite_the_state_file(tmp_path, sent, monkeypatch):
    run = _Run(tmp_path).make(recorder="STALE")
    run.store(("STALE", "OK"))  # outage 3 days old, last delivered alert 1 h ago
    writes = []
    monkeypatch.setattr(probe, "save_alert_state", lambda path, state: writes.append(state))

    assert run.run("--quiet") == 1

    assert sent.messages == []
    assert writes == []


def test_reminder_is_sent_once_the_last_delivered_alert_is_24_hours_old(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")
    run.store(
        ("STALE", "OK"), last_alert_utc=(run.now() - timedelta(hours=25)).isoformat()
    )
    seeded = run.stored()

    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[still STALE, 3d 0h] ibkr recorder STALE:")
    stored = run.stored()
    assert stored["since_utc"] == seeded["since_utc"]  # the pair did not change
    assert stored["outage_since_utc"] == seeded["outage_since_utc"]  # nor did the outage
    assert _ts(stored["last_alert_utc"]) > _ts(seeded["last_alert_utc"]) + timedelta(hours=24)
    assert stored["last_alert_ok"] is True
    ((kind, status),) = [(m["kind"], m["status"]) for m in map(SENT_LINE.match, run.alert_lines())]
    assert (kind, status) == ("reminder", "STALE")

    # The reminder restarted the 24 h clock: the next hourly run is silent again.
    assert run.run("--quiet") == 1
    assert len(sent.messages) == 1


@pytest.mark.parametrize(
    ("age", "expected_messages"),
    [
        (timedelta(hours=23, minutes=59, seconds=59), 0),
        (timedelta(hours=24), 1),
        (timedelta(hours=24, seconds=1), 1),
    ],
    ids=["23:59:59", "24:00:00", "24:00:01"],
)
def test_main_applies_the_24_hour_boundary_to_its_own_clock(
    tmp_path, sent, monkeypatch, age, expected_messages
):
    run = _Run(tmp_path, clock=T0).make(recorder="STALE")
    run.store(("STALE", "OK"), last_alert_utc=(T0 - age).isoformat())
    _freeze_clock(monkeypatch, T0)

    assert run.run("--quiet") == 1

    assert len(sent.messages) == expected_messages


def test_undelivered_alert_is_retried_on_every_run_until_it_gets_through(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")

    sent.outcome = False
    assert run.run("--quiet") == 1  # the transition does not get through
    assert run.stored()["last_alert_ok"] is False
    assert run.run("--quiet") == 1  # retried, still not through
    sent.outcome = True
    assert run.run("--quiet") == 1  # retried, delivered
    assert run.stored()["last_alert_ok"] is True
    assert run.run("--quiet") == 1  # delivered: back to silence

    prefixes = [message.split("]")[0] + "]" for message in sent.messages]
    assert prefixes == ["[OK -> STALE]", "[still STALE, 0m]", "[still STALE, 0m]"]
    alert_lines = run.alert_lines()
    reported = [(m["kind"], m["status"]) for m in map(SENT_LINE.match, alert_lines) if m]
    failed = [
        (m["kind"], m["status"], m["reason"]) for m in map(FAILED_LINE.match, alert_lines) if m
    ]
    assert failed == [
        ("transition", "STALE", "not_delivered"),
        ("retry", "STALE", "not_delivered"),
    ]
    assert reported == [("retry", "STALE")]
    # Fourth run was silent, so the durable log holds 4 status lines + 3 alert lines.
    assert len(run.status_lines()) == 4
    assert len(run.alert_lines()) == 3


def test_recovery_sends_one_transition_alert_and_clears_the_outage(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="OK")
    now = datetime.now(timezone.utc)
    run.store(
        ("STALE", "OK"),
        outage_since_utc=(now - timedelta(days=4, hours=12, minutes=5)).isoformat(),
    )

    rc = run.run("--quiet")

    assert rc == 0
    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[STALE -> OK] recovered after 4d 12h: ibkr recorder OK:")
    stored = run.stored()
    assert (stored["recorder_status"], stored["sync_status"]) == ("OK", "OK")
    assert stored["outage_since_utc"] is None
    assert stored["since_utc"] == stored["last_alert_utc"]
    assert stored["last_alert_ok"] is True
    # The one OK run that writes to the durable log: its alert line, no status line.
    (line,) = run.log_lines()
    assert SENT_LINE.match(line)["kind"] == "transition"
    assert SENT_LINE.match(line)["status"] == "OK"

    # Healthy and unchanged from here on: nothing sent, nothing written.
    assert run.run("--quiet") == 0
    assert len(sent.messages) == 1
    assert len(run.log_lines()) == 1


def test_failed_recovery_alert_is_recorded_but_not_retried(tmp_path, sent):
    """The retry rule covers an unhealthy state only ("the previous send attempt
    failed AND the state is not all-OK"): a recovery message that did not get
    through is logged as ALERT_FAILED and never repeated. This pins that
    reading of the rule; relaxing it is a one-line change in decide_alert."""
    run = _Run(tmp_path).make(recorder="OK")
    run.store(("STALE", "OK"))
    sent.outcome = False

    assert run.run("--quiet") == 0  # recovery: attempted, not delivered
    assert run.run("--quiet") == 0  # healthy and unchanged: nothing more is sent

    assert len(sent.messages) == 1
    assert run.stored()["last_alert_ok"] is False
    (line,) = run.log_lines()
    match = FAILED_LINE.match(line)
    got = (match["kind"], match["status"], match["reason"])
    assert got == ("transition", "OK", "not_delivered")


def test_change_of_kind_sends_a_transition_and_the_outage_keeps_its_start(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE", sync="SYNC_STALE")
    run.store(("STALE", "OK"))
    seeded_outage_start = run.stored()["outage_since_utc"]

    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[STALE -> STALE+SYNC_STALE] ibkr recorder STALE:")
    assert "ibkr paper sync SYNC_STALE" in sent.messages[0]
    stored = run.stored()
    assert (stored["recorder_status"], stored["sync_status"]) == ("STALE", "SYNC_STALE")
    assert stored["outage_since_utc"] == seeded_outage_start
    assert stored["since_utc"] == stored["last_alert_utc"]  # the pair changed in this run


def test_alert_lines_carry_the_combined_status_recorder_axis_first(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="DOWN", sync="SYNC_STALE")

    assert run.run("--quiet") == 1

    (line,) = run.alert_lines()
    assert SENT_LINE.match(line)["status"] == "DOWN"
    sync_only = _Run(tmp_path / "second").make(recorder="OK", sync="SYNC_STALE")
    assert sync_only.run("--quiet") == 1
    (line,) = sync_only.alert_lines()
    assert SENT_LINE.match(line)["status"] == "SYNC_STALE"


def test_healthy_run_without_a_state_file_is_silent_and_leaves_no_trace(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="OK")

    assert run.run("--quiet") == 0

    assert sent.messages == []
    assert not run.alert_state.exists()
    assert not run.health_log.exists()


def test_corrupt_state_file_fails_toward_alerting(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")
    run.alert_state.parent.mkdir(parents=True)
    run.alert_state.write_text("{this is not json", encoding="utf-8")

    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[OK -> STALE]")
    assert run.stored()["recorder_status"] == "STALE"  # replaced by a valid file


def test_healthy_run_with_a_corrupt_state_file_stays_silent(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="OK")
    run.alert_state.parent.mkdir(parents=True)
    run.alert_state.write_bytes(b"\xff\xfe corrupt")

    assert run.run("--quiet") == 0

    assert sent.messages == []
    assert run.alert_state.read_bytes() == b"\xff\xfe corrupt"  # not touched, not "healed"


def test_stdout_keeps_the_plain_health_message_and_the_alert_adds_a_prefix(
    tmp_path, sent, capsys
):
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run() == 1

    out = capsys.readouterr().out
    assert out.startswith("ibkr recorder STALE: heartbeat is 120 min old")
    assert sent.messages == [f"[OK -> STALE] {out.strip()}"]
    assert sent.messages[0].isascii() and "\n" not in sent.messages[0]


def test_quiet_recovery_prints_nothing_but_still_alerts(tmp_path, sent, capsys):
    run = _Run(tmp_path).make(recorder="OK")
    run.store(("STALE", "OK"))

    assert run.run("--quiet") == 0

    assert capsys.readouterr() == ("", "")
    assert len(sent.messages) == 1


def test_help_lists_the_new_options(capsys):
    """argparse %-formats every help string: a stray percent sign in one would
    only blow up when someone asks for --help."""
    with pytest.raises(SystemExit) as excinfo:
        probe.main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--alert-state-path" in out
    assert "--test-alert" in out


def test_default_alert_state_path_lives_beside_the_durable_log():
    expected = probe.REPO_ROOT / "logs" / "ibkr_health_alert_state.json"
    assert probe.DEFAULT_ALERT_STATE_PATH == expected
    assert probe.DEFAULT_ALERT_STATE_PATH.parent == probe.DEFAULT_HEALTH_LOG_PATH.parent


def test_main_falls_back_to_the_default_alert_state_path(tmp_path, sent, monkeypatch):
    default = tmp_path / "default_alert_state.json"
    monkeypatch.setattr(probe, "DEFAULT_ALERT_STATE_PATH", default)
    run = _Run(tmp_path).make(recorder="STALE")

    assert probe.main(run.argv("--quiet", with_alert_state=False)) == 1

    assert json.loads(default.read_text(encoding="utf-8"))["recorder_status"] == "STALE"
    assert not run.alert_state.exists()


def test_one_run_reads_the_clock_once_and_stamps_everything_with_that_instant(
    tmp_path, sent, monkeypatch
):
    run = _Run(tmp_path, clock=T0).make(recorder="STALE")
    clock = _freeze_clock(monkeypatch, T0)

    assert run.run("--quiet") == 1

    assert clock.calls == 1
    stored = run.stored()
    assert {stored["since_utc"], stored["last_alert_utc"], stored["outage_since_utc"]} == {
        T0.isoformat()
    }
    lines = run.log_lines()
    assert len(lines) == 2  # the status line and the transition's ALERT_SENT line
    assert all(line.startswith(T0.isoformat() + " ") for line in lines)


# -- durable log format ----------------------------------------------------------


def test_alert_lines_have_the_documented_shape(tmp_path, sent):
    run = _Run(tmp_path).make(recorder="STALE")
    sent.outcome = False
    run.run("--quiet")
    sent.outcome = True
    run.run("--quiet")

    lines = run.alert_lines()
    assert len(lines) == 2  # a failed transition, then a delivered retry
    for line in lines:
        match = SENT_LINE.match(line) or FAILED_LINE.match(line)
        assert match, line
        assert _ts(match["ts"]).utcoffset() == timedelta(0)
    ts, status, rest = run.status_lines()[0].split(" ", 2)  # the pre-existing format, unchanged
    assert status == "STALE"
    assert rest.startswith("ibkr recorder STALE:")
    _ts(ts)


# -- exit codes and channel independence ---------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [True, False, RuntimeError("telegram exploded")],
    ids=["delivered", "refused", "raised"],
)
def test_exit_code_follows_health_alone_never_the_alert_outcome(tmp_path, sent, outcome):
    sent.outcome = outcome
    run = _Run(tmp_path).make(recorder="STALE")
    assert run.run("--quiet") == 1

    run.make(recorder="OK")  # recovered: its alert goes through the same outcome
    assert run.run("--quiet") == 0

    assert len(sent.messages) == 2


@pytest.mark.parametrize("answer", [None, 0, "sent"], ids=["none", "zero", "truthy-string"])
def test_only_an_explicit_true_counts_as_delivered(tmp_path, sent, answer):
    """A stub (or a future bug) answering with anything but True must not be
    recorded as a delivery: the alert is retried rather than lost."""
    sent.outcome = answer
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run("--quiet") == 1

    assert run.stored()["last_alert_ok"] is False
    assert FAILED_LINE.match(run.alert_lines()[0])["reason"] == "not_delivered"


def test_send_alert_raising_is_logged_by_class_name_and_retried(tmp_path, sent, capsys):
    sent.outcome = RuntimeError(f"Max retries exceeded with url: /bot{FAKE_TOKEN}/sendMessage")
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run() == 1  # NOT quiet: the stderr note is visible

    (line,) = run.alert_lines()
    assert FAILED_LINE.match(line)["reason"] == "RuntimeError"
    assert run.stored()["last_alert_ok"] is False
    captured = capsys.readouterr()
    assert "send_alert failed (RuntimeError)" in captured.err
    assert FAKE_TOKEN not in captured.out + captured.err + line

    sent.outcome = True
    assert run.run("--quiet") == 1
    assert len(sent.messages) == 2  # retried on the very next run
    assert FAILED_LINE.match(run.alert_lines()[0])
    assert SENT_LINE.match(run.alert_lines()[1])["kind"] == "retry"


@pytest.mark.parametrize(
    ("recorder", "expected_rc"), [("STALE", 1), ("OK", 0)], ids=["unhealthy", "recovery"]
)
def test_state_write_failure_never_stops_the_alert_or_changes_the_exit_code(
    tmp_path, sent, monkeypatch, capsys, recorder, expected_rc
):
    def _boom(path, state):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(probe, "save_alert_state", _boom)
    run = _Run(tmp_path).make(recorder=recorder)
    if recorder == "OK":
        run.store(("STALE", "OK"))

    assert run.run() == expected_rc  # NOT quiet: the note is visible

    assert len(sent.messages) == 1
    err = capsys.readouterr().err
    assert "alert state write failed (OSError)" in err
    assert "disk full" not in err  # class name only


def test_unwritable_state_location_alerts_every_run_and_exits_as_before(tmp_path, sent):
    """The cost of a state file that can never be written is the old behaviour
    (an alert per unhealthy run), not a crash and not silence."""
    run = _Run(tmp_path).make(recorder="STALE")
    run.alert_state.mkdir(parents=True)  # a directory where the file should be

    assert run.run("--quiet") == 1
    assert run.run("--quiet") == 1

    assert len(sent.messages) == 2
    assert all(message.startswith("[OK -> STALE]") for message in sent.messages)


def test_state_read_failure_never_stops_the_alert(tmp_path, sent, monkeypatch):
    def _boom(path):
        raise RuntimeError("unexpected failure while reading the state (simulated)")

    monkeypatch.setattr(probe, "load_alert_state", _boom)
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1
    assert sent.messages[0].startswith("[OK -> STALE]")


def test_alert_log_failure_never_stops_the_state_update_or_changes_the_exit_code(
    tmp_path, sent, monkeypatch, capsys
):
    def _boom(log_path, kind, status, delivered, reason, now):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(probe, "_append_alert_log", _boom)
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run() == 1

    assert len(sent.messages) == 1
    assert run.stored()["last_alert_ok"] is True
    assert "alert log write failed (OSError)" in capsys.readouterr().err


def test_status_line_failure_does_not_stop_the_alert_line_or_the_state_update(
    tmp_path, sent, monkeypatch
):
    def _boom(log_path, status, message, now=None):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(probe, "_append_health_log", _boom)
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run("--quiet") == 1

    assert len(sent.messages) == 1
    assert run.stored()["last_alert_ok"] is True
    (line,) = run.log_lines()
    assert SENT_LINE.match(line)["kind"] == "transition"


@pytest.mark.parametrize("failing", ["send_alert", "alert_log", "state_write"])
def test_quiet_silences_every_failure_note(tmp_path, sent, monkeypatch, capsys, failing):
    def _boom(*args, **kwargs):
        raise OSError("simulated")

    if failing == "send_alert":
        sent.outcome = RuntimeError("telegram exploded")
    elif failing == "alert_log":
        monkeypatch.setattr(probe, "_append_alert_log", _boom)
    else:
        monkeypatch.setattr(probe, "save_alert_state", _boom)
    run = _Run(tmp_path).make(recorder="STALE")

    assert run.run("--quiet") == 1

    assert capsys.readouterr() == ("", "")
    assert len(sent.messages) == 1  # still alerted


# --------------------------------------------------------------------------- #
# 4. --test-alert: a labelled message through the normal path, on demand
# --------------------------------------------------------------------------- #

TEST_MESSAGE = re.compile(
    r"^TEST ibkr health alert, sent \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -- no action needed$"
)


def test_test_alert_sends_one_labelled_ascii_line_and_logs_a_test_line(
    tmp_path, sent, capsys
):
    run = _Run(tmp_path)  # deliberately no inputs: a health check would report CRITICAL

    rc = probe.main(run.argv("--test-alert"))

    assert rc == 0
    (message,) = sent.messages
    assert TEST_MESSAGE.match(message)
    assert message.isascii()
    (line,) = run.log_lines()  # no status line: no health check ran
    match = SENT_LINE.match(line)
    assert (match["kind"], match["status"]) == ("test", "-")
    out = capsys.readouterr().out
    assert "delivered" in out and "NOT" not in out


def test_test_alert_stamps_message_and_log_with_the_run_clock(tmp_path, sent, monkeypatch):
    run = _Run(tmp_path, clock=T0)
    clock = _freeze_clock(monkeypatch, T0)

    assert probe.main(run.argv("--test-alert", "--quiet")) == 0

    assert clock.calls == 1
    assert sent.messages == [
        "TEST ibkr health alert, sent 2026-10-03T12:00:00Z -- no action needed"
    ]
    assert run.log_lines() == [f"{T0.isoformat()} ALERT_SENT test -"]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [(False, "not_delivered"), (RuntimeError("telegram exploded"), "RuntimeError")],
    ids=["refused", "raised"],
)
def test_test_alert_failure_exits_1_and_logs_the_failure(
    tmp_path, sent, capsys, outcome, reason
):
    sent.outcome = outcome
    run = _Run(tmp_path)

    rc = probe.main(run.argv("--test-alert"))

    assert rc == 1
    (line,) = run.log_lines()
    match = FAILED_LINE.match(line)
    assert (match["kind"], match["status"], match["reason"]) == ("test", "-", reason)
    assert "NOT delivered" in capsys.readouterr().out


def test_test_alert_neither_reads_nor_writes_the_alert_state_file(tmp_path, sent, monkeypatch):
    reads, writes = [], []
    monkeypatch.setattr(probe, "load_alert_state", lambda path: reads.append(path))
    monkeypatch.setattr(probe, "save_alert_state", lambda path, state: writes.append(path))
    run = _Run(tmp_path)
    run.store(("DOWN", "OK"))  # a live outage that a test alert must leave alone
    before = run.alert_state.read_bytes()

    assert probe.main(run.argv("--test-alert", "--quiet")) == 0

    assert (reads, writes) == ([], [])
    assert run.alert_state.read_bytes() == before


def test_test_alert_creates_no_state_file_and_ignores_a_corrupt_one(tmp_path, sent):
    run = _Run(tmp_path)
    assert probe.main(run.argv("--test-alert", "--quiet")) == 0
    assert not run.alert_state.exists()

    run.alert_state.write_text("{corrupt", encoding="utf-8")
    assert probe.main(run.argv("--test-alert", "--quiet")) == 0
    assert run.alert_state.read_text(encoding="utf-8") == "{corrupt"


def test_test_alert_runs_no_health_check(tmp_path, sent, monkeypatch):
    checks = []
    monkeypatch.setattr(probe, "run_check", lambda *a, **k: checks.append("recorder"))
    monkeypatch.setattr(probe, "_check_sync_freshness", lambda *a, **k: checks.append("sync"))
    run = _Run(tmp_path)

    assert probe.main(run.argv("--test-alert", "--quiet")) == 0

    assert checks == []
    assert len(run.log_lines()) == 1  # the ALERT line only, no per-run status line


def test_test_alert_honours_quiet_for_stdout_but_still_sends(tmp_path, sent, capsys):
    run = _Run(tmp_path)

    assert probe.main(run.argv("--test-alert", "--quiet")) == 0

    assert capsys.readouterr() == ("", "")
    assert len(sent.messages) == 1


@pytest.mark.parametrize("failing", ["send_alert", "alert_log"])
def test_test_alert_failure_notes_are_silent_under_quiet(
    tmp_path, sent, monkeypatch, capsys, failing
):
    def _boom(*args, **kwargs):
        raise OSError("simulated")

    if failing == "send_alert":
        sent.outcome = RuntimeError("telegram exploded")
    else:
        monkeypatch.setattr(probe, "_append_alert_log", _boom)

    rc = probe.main(_Run(tmp_path).argv("--test-alert", "--quiet"))

    assert rc == (1 if failing == "send_alert" else 0)
    assert capsys.readouterr() == ("", "")


def test_test_alert_log_write_failure_does_not_change_the_outcome(
    tmp_path, sent, monkeypatch, capsys
):
    def _boom(log_path, kind, status, delivered, reason, now):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(probe, "_append_alert_log", _boom)

    assert probe.main(_Run(tmp_path).argv("--test-alert")) == 0  # delivered

    assert len(sent.messages) == 1
    assert "alert log write failed (OSError)" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 5. Secret hygiene. Telegram's API takes the bot token in the URL PATH
# (/bot<TOKEN>/sendMessage) and a connection error quotes that path verbatim.
# The probe's stdout/stderr are appended to logs/ibkr_health_task.log, and the
# new durable lines and the alert state file are files on a synced disk too, so
# the token must reach none of them. These tests run the REAL send_alert with a
# FAKE token against a requests.post that fails with the full URL in its text.
# --------------------------------------------------------------------------- #


def _leaky_text() -> str:
    return (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage (Caused by "
        "NewConnectionError('<urllib3.connection.HTTPSConnection object>: Failed to "
        "establish a new connection: [Errno 11001] getaddrinfo failed'))"
    )


class _FakePost:
    """requests.post stand-in. ``failure`` is an exception type raised with the
    leaky text; None answers HTTP 200. Records the URL of every call."""

    def __init__(self, failure=None) -> None:
        self.failure = failure
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        if self.failure is not None:
            raise self.failure(_leaky_text())
        return SimpleNamespace(status_code=200)


def _telegram(monkeypatch, failure=requests.ConnectionError) -> _FakePost:
    """The real Telegram path, fully configured with FAKE values; only the network is replaced."""
    monkeypatch.setenv("ALERT_METHOD", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    post = _FakePost(failure)
    monkeypatch.setattr(heartbeat_mod.requests, "post", post)
    return post


def _all_logged_text(caplog) -> str:
    """Every rendered log line plus every record's formatted exc_info/stack, so
    a traceback-carried leak is caught too, not just the message."""
    parts = [caplog.text]
    formatter = logging.Formatter()
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(formatter.formatException(record.exc_info))
        if record.stack_info:
            parts.append(record.stack_info)
    return "\n".join(parts)


def _assert_token_nowhere(run: _Run, captured, caplog) -> None:
    def _text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    secret_half = FAKE_TOKEN.split(":", 1)[1]
    where = {
        "stdout": captured.out,
        "stderr": captured.err,
        "log records": _all_logged_text(caplog),
        "durable log": _text(run.health_log),
        "alert state": _text(run.alert_state),
    }
    for name, text in where.items():
        assert FAKE_TOKEN not in text, f"bot token leaked into {name}"
        assert secret_half not in text, f"the secret half of the bot token leaked into {name}"


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.SSLError,
        ValueError,
    ],
)
def test_token_reaches_no_output_on_the_normal_path(tmp_path, monkeypatch, caplog, capsys, failure):
    post = _telegram(monkeypatch, failure)
    assert FAKE_TOKEN in _leaky_text()  # the exception really does carry the token
    run = _Run(tmp_path).make(recorder="STALE")

    with caplog.at_level(logging.DEBUG):
        rc = run.run()  # NOT quiet: the stderr notes are exercised too

    assert rc == 1
    assert len(post.urls) == 1  # the real send_alert ran, up to the failing post
    (line,) = run.alert_lines()
    assert FAILED_LINE.match(line)["kind"] == "transition"
    assert run.stored()["last_alert_ok"] is False
    assert any(r.levelno == logging.CRITICAL and "telegram send" in r.getMessage()
               for r in caplog.records)  # send_alert's own fallback line fired ...
    _assert_token_nowhere(run, capsys.readouterr(), caplog)  # ... and is clean


def test_token_reaches_no_output_on_the_retry_path(tmp_path, monkeypatch, caplog, capsys):
    post = _telegram(monkeypatch)
    run = _Run(tmp_path).make(recorder="STALE")
    run.store(("STALE", "OK"), last_alert_ok=False)  # the previous attempt did not get through

    with caplog.at_level(logging.DEBUG):
        rc = run.run()

    assert rc == 1
    assert len(post.urls) == 1
    (line,) = run.alert_lines()
    assert FAILED_LINE.match(line)["kind"] == "retry"
    _assert_token_nowhere(run, capsys.readouterr(), caplog)


def test_token_reaches_no_output_on_the_test_alert_path(tmp_path, monkeypatch, caplog, capsys):
    post = _telegram(monkeypatch)
    run = _Run(tmp_path)

    with caplog.at_level(logging.DEBUG):
        rc = probe.main(run.argv("--test-alert"))

    assert rc == 1
    assert len(post.urls) == 1
    (line,) = run.alert_lines()
    assert FAILED_LINE.match(line)["kind"] == "test"
    assert not run.alert_state.exists()
    _assert_token_nowhere(run, capsys.readouterr(), caplog)


def test_token_is_only_ever_in_the_request_url_when_the_send_succeeds(
    tmp_path, monkeypatch, caplog, capsys
):
    post = _telegram(monkeypatch, failure=None)  # HTTP 200
    run = _Run(tmp_path).make(recorder="STALE")

    with caplog.at_level(logging.DEBUG):
        rc = run.run()

    assert rc == 1
    # The API needs the token in the URL: that is the one place it may appear.
    assert post.urls == [f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"]
    (line,) = run.alert_lines()
    assert SENT_LINE.match(line)["kind"] == "transition"
    assert run.stored()["last_alert_ok"] is True
    _assert_token_nowhere(run, capsys.readouterr(), caplog)
