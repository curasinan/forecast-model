"""scripts/spark/cutover_check.py: the four lines of the cutover gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import cutover_check  # noqa: E402

SINCE = "2026-10-20T16:00:00+00:00"
LONG_BEFORE = "2026-10-18T14:30:00+00:00"
MID_BEFORE = "2026-10-18T20:00:00+00:00"
BEFORE = "2026-10-19T14:30:00+00:00"
JUST_BEFORE = "2026-10-20T15:59:59+00:00"
AFTER_EARLY = "2026-10-20T16:05:00+00:00"
AFTER_MID = "2026-10-20T16:10:00+00:00"
SWEEP_AT = "2026-10-20T16:15:00+00:00"
AFTER = "2026-10-20T16:20:00+00:00"
# Clearly synthetic. A real ledger line carries the paper account id, and no run may print it.
ACCOUNT = "DU0000000"
COUNTS = {"examined": 3, "filled": 0, "partially_filled": 0, "cancelled": 0, "unchanged": 3,
          "unmatched_venue_orders": 0}
SYSTEMCTL = ["systemctl", "show", "ibkr-health.service", "--timestamp=unix",
             "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"]
ZENBOOK = {"python": "3.14.2"}
SPARK = {"python": "3.14.2", "machine": "aarch64"}


def _sync(ts: str, ok: bool = True, **changes) -> str:
    """One line of the sync ledger with the fields scripts/sync_ibkr_paper.py writes."""
    record = {"ts_utc": ts, "ok": ok, "aborted": False, **COUNTS, "skipped_missing_perm_id": 0,
              "notes": [], "account": ACCOUNT, "db": "/srv/quant_data/ibkr_ticks/paper/x.db"}
    return json.dumps({**record, **changes})


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()


def _systemd(status: int, exited: str | None, raw: str | None = None, calls: list | None = None):
    """A stand-in for subprocess.run that answers `systemctl show ibkr-health.service ...`.

    `raw` replaces the exit-time text outright; `calls` collects (argv, kwargs) per call.
    """

    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        assert cmd[:3] == ["systemctl", "show", "ibkr-health.service"], cmd
        if raw is not None:
            stamp = raw
        else:
            stamp = "" if exited is None else f"@{int(_epoch(exited))}"
        text = f"ExecMainExitTimestamp={stamp}\nExecMainStatus={status}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    return run


def _write_sweep(root: Path, stamp: str, rows: int = 2400, mtime: str | None = None) -> Path:
    """A sweep file named for `stamp` the way the recorder names it (UTC date and time).

    Its mtime is `mtime`, by default the same moment: a file written when its name says.
    """
    when = datetime.fromisoformat(stamp)
    path = (root / "ibkr_ticks" / "quotes" / f"date={when:%Y-%m-%d}"
            / f"quotes-{when:%H%M%S}Z.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"con_id": range(rows), "bid": [0.4] * rows}).to_parquet(path, index=False)
    os.utime(path, (_epoch(mtime or stamp), _epoch(mtime or stamp)))
    return path


def _scores(machine: dict | None, leaves: int = 1200, probe: float | None = None) -> dict:
    """A parity payload: `leaves` numbers, an optional probe number, an identity.machine block."""
    payload: dict = {"x": [float(i) for i in range(leaves)]}
    if probe is not None:
        payload["probe"] = probe
    if machine is not None:
        payload["identity"] = {"machine": machine}
    return payload


def _pair(site, left: dict, right: dict) -> None:
    site["left"].write_text(json.dumps(left), encoding="utf-8")
    site["right"].write_text(json.dumps(right), encoding="utf-8")


def _ledger(site, *lines: str) -> None:
    site["ledger"].write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


@pytest.fixture
def site(tmp_path):
    """A live root and a repo directory in the state a good cutover leaves them in."""
    root = tmp_path / "quant_data"
    sweep = _write_sweep(root, SWEEP_AT)
    ledger = root / "ibkr_ticks" / "paper" / "ibkr_sync_runs.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(_sync(BEFORE) + "\n" + _sync(AFTER) + "\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "logs").mkdir(parents=True)
    # A healthy run writes nothing here; the DOWN line is from before the cutover.
    health = repo / "logs" / "ibkr_health.log"
    health.write_text(
        f"{BEFORE} DOWN ibkr recorder DOWN: unexpected disconnect\n"
        f"{AFTER} ALERT_SENT test - telegram\n", encoding="utf-8")
    left = tmp_path / "zenbook.json"
    right = tmp_path / "spark.json"
    left.write_text(json.dumps(_scores(ZENBOOK)), encoding="utf-8")
    right.write_text(json.dumps(_scores(SPARK)), encoding="utf-8")
    return {"root": root, "repo": repo, "sweep": sweep, "ledger": ledger, "health": health,
            "left": left, "right": right}


def _assert_quiet(out: str, err: str) -> None:
    """No run, whatever its verdict, leaves a traceback in stderr or prints the account id."""
    assert "Traceback" not in err, err
    assert ACCOUNT not in out + err


def _run(site, capsys, systemd=None, parity: bool = True, since: str = SINCE) -> tuple[int, str]:
    """Run the gate once and return (exit code, stdout); every run is also checked quiet."""
    argv = ["--root", str(site["root"]), "--repo", str(site["repo"]), "--since", since]
    if parity:
        argv += ["--parity", str(site["left"]), str(site["right"])]
    rc = cutover_check.main(argv, run=systemd or _systemd(0, AFTER))
    seen = capsys.readouterr()
    _assert_quiet(seen.out, seen.err)
    return rc, seen.out


def _only_failed(out: str, name: str, detail: str) -> None:
    """Exactly one verdict line failed, with exactly `detail`; the other three still printed."""
    lines = out.splitlines()
    assert [line.split()[1] for line in lines[:4]] == ["sweep:", "health:", "sync:", "parity:"]
    for line in lines[:4]:
        expected = "FAIL" if line.split()[1] == f"{name}:" else "PASS"
        assert line.startswith(f"{expected} "), out
    assert f"FAIL {name}: {detail}" in lines
    assert lines[-1] == "CUTOVER GATE: FAIL -- roll back"


def test_a_good_cutover_passes_every_line(site, capsys):
    rc, out = _run(site, capsys)
    assert rc == 0, out
    lines = out.splitlines()
    assert [line.split()[0] for line in lines[:4]] == ["PASS"] * 4
    assert [line.split()[1] for line in lines[:4]] == ["sweep:", "health:", "sync:", "parity:"]
    assert lines[-1] == "CUTOVER GATE: PASS"
    # compare_json's own output follows the parity line, every line indented four spaces:
    # the leaf count, the two machines it compared, and its verdict.
    shown = lines[4:-1]
    assert shown and all(line.startswith("    ") for line in shown), out
    assert shown[0].startswith("    numeric leaves compared: 1200;")
    # The first --parity payload is the Zenbook's (left), the second the Spark's (right).
    assert "    machines: left py3.14.2 vs right aarch64/py3.14.2" in shown
    assert shown[-1] == "    PARITY OK"


# ---------------------------------------------------------------------------------- sweep


def test_a_sweep_named_for_a_time_before_the_cutover_does_not_count(site, capsys):
    site["sweep"].unlink()
    _write_sweep(site["root"], BEFORE)
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: no sweep file written since the cutover" in out
    assert "CUTOVER GATE: FAIL" in out


def test_a_sweep_is_dated_by_its_own_name_not_by_when_it_was_copied(site, capsys):
    # A root the Zenbook's files were copied into: the copies are all freshly modified.
    site["sweep"].unlink()
    _write_sweep(site["root"], BEFORE, mtime=AFTER)
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: no sweep file written since the cutover" in out


@pytest.mark.parametrize("name", ["sweep.parquet", "quotes-996161Z.parquet"])
def test_a_sweep_whose_name_carries_no_time_is_dated_by_its_mtime(site, capsys, name):
    site["sweep"].unlink()
    odd = site["sweep"].with_name(name)  # no recorder time in it, or an impossible one
    pd.DataFrame({"con_id": range(2400)}).to_parquet(odd, index=False)
    os.utime(odd, (_epoch(AFTER), _epoch(AFTER)))
    rc, out = _run(site, capsys)
    assert rc == 0, out

    os.utime(odd, (_epoch(BEFORE), _epoch(BEFORE)))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: no sweep file written since the cutover" in out


def test_a_thin_sweep_does_not_count_whatever_older_sweeps_hold(site, capsys):
    _write_sweep(site["root"], BEFORE)  # the Zenbook's old sweeps are big
    _write_sweep(site["root"], SWEEP_AT, rows=1999)
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: quotes-161500Z.parquet has 1999 rows, under 2000" in out


def test_a_sweep_of_exactly_2000_rows_counts(site, capsys):
    _write_sweep(site["root"], SWEEP_AT, rows=2000)
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert "PASS sweep: quotes-161500Z.parquet, 2000 rows" in out


def test_only_the_newest_sweep_since_the_cutover_is_judged(site, capsys):
    _write_sweep(site["root"], AFTER_EARLY, rows=1999)  # fresh, thin, and older than the 2400
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert "PASS sweep: quotes-161500Z.parquet, 2400 rows" in out

    _write_sweep(site["root"], AFTER, rows=1999)  # now the newest is thin
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: quotes-162000Z.parquet has 1999 rows, under 2000" in out


def test_a_sweep_newer_in_a_later_date_partition_is_the_newest(site, capsys):
    _write_sweep(site["root"], "2026-10-20T23:59:00+00:00")
    _write_sweep(site["root"], "2026-10-21T00:01:00+00:00", rows=1999)
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep: quotes-000100Z.parquet has 1999 rows" in out


def test_an_unreadable_newest_sweep_fails_its_line_and_hides_nothing(site, capsys):
    torn = site["sweep"].with_name("quotes-171500Z.parquet")
    torn.write_bytes(b"")  # a zero-byte sweep: the newest by name and by mtime
    os.utime(torn, (_epoch(AFTER), _epoch(AFTER)))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sweep", "could not be evaluated (ArrowInvalid)")


# --------------------------------------------------------------------------------- health


def test_a_probe_that_found_a_problem_fails_the_gate_and_says_which(site, capsys):
    with site["health"].open("a", encoding="utf-8") as log:
        log.write(f"{AFTER} STALE ibkr recorder STALE: heartbeat is 70 min old\n")
    rc, out = _run(site, capsys, systemd=_systemd(1, AFTER))
    assert rc == 1
    assert "FAIL health" in out and "exit status is 1" in out and "STALE" in out


def test_a_problem_from_before_the_cutover_is_not_quoted_as_the_reason(site, capsys):
    # The fixture's log holds a DOWN line from before the cutover and no problem since.
    rc, out = _run(site, capsys, systemd=_systemd(1, AFTER))
    assert rc == 1
    assert "FAIL health: the probe's last exit status is 1" in out
    assert "DOWN" not in out


def test_the_reason_quoted_is_the_last_status_line_never_an_alert_line(site, capsys):
    site["health"].write_text(
        f"{AFTER_EARLY} DOWN ibkr recorder DOWN: unexpected disconnect\n"
        f"{AFTER_EARLY} ALERT_SENT transition DOWN telegram\n"
        f"{AFTER} STALE ibkr recorder STALE: heartbeat is 70 min old\n"
        f"{AFTER} ALERT_FAILED transition STALE telegram URLError\n"
        f"{AFTER} ALERT_SENT test - telegram\n", encoding="utf-8")
    rc, out = _run(site, capsys, systemd=_systemd(1, AFTER))
    assert rc == 1
    assert ("FAIL health: the probe's last exit status is 1: "
            "STALE ibkr recorder STALE: heartbeat is 70 min old") in out.splitlines()


def test_a_probe_that_has_not_run_since_the_cutover_does_not_count(site, capsys):
    rc, out = _run(site, capsys, systemd=_systemd(0, BEFORE))
    assert rc == 1
    assert "FAIL health" in out and "has not finished a run since the cutover" in out

    rc, out = _run(site, capsys, systemd=_systemd(0, None))  # the unit never ran at all
    assert rc == 1
    assert "has not finished a run since the cutover" in out

    rc, out = _run(site, capsys, systemd=_systemd(0, None, raw="n/a"))  # or systemd says n/a
    assert rc == 1
    assert "has not finished a run since the cutover" in out


def test_the_health_unit_is_asked_for_exactly_its_exit_status_and_exit_time(site, capsys):
    calls: list = []
    rc, out = _run(site, capsys, systemd=_systemd(0, AFTER, calls=calls))
    assert rc == 0, out
    assert [argv for argv, _kwargs in calls] == [SYSTEMCTL]
    kwargs = calls[0][1]
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] > 0  # a hung systemctl must not hang the gate


def test_systemd_that_cannot_be_asked_is_not_read_as_healthy(site, capsys):
    def no_systemctl(cmd, **_kwargs):
        raise FileNotFoundError("systemctl")

    rc, out = _run(site, capsys, systemd=no_systemctl)
    assert rc == 1
    assert "FAIL health" in out and "could not ask systemd" in out


def test_a_systemctl_that_fails_is_not_read_as_healthy_and_says_why(site, capsys):
    def failing(cmd, **_kwargs):
        stderr = "Failed to connect to bus: No medium found\nsecond line\n"
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    rc, out = _run(site, capsys, systemd=failing)
    assert rc == 1
    assert "FAIL health" in out and "could not ask systemd" in out
    assert "systemctl exit 1: Failed to connect to bus: No medium found" in out
    assert "second line" not in out  # the first line of its stderr, no more

    def silent(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="")

    rc, out = _run(site, capsys, systemd=silent)
    assert rc == 1
    assert "could not ask systemd" in out and "systemctl exit 3" in out


@pytest.mark.parametrize("line", [
    "ALERT_SENT test - log_only",             # delivered, but only to the log
    "ALERT_SENT transition STALE telegram",   # a real alert, not the test alert
    "ALERT_FAILED test - telegram URLError",  # the test alert did not get through
])
def test_only_a_test_alert_delivered_by_telegram_counts(site, capsys, line):
    site["health"].write_text(f"{AFTER} {line}\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL health" in out and "no delivered test alert" in out


def test_a_test_alert_from_before_the_cutover_does_not_count(site, capsys):
    site["health"].write_text(f"{BEFORE} ALERT_SENT test - telegram\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL health: no delivered test alert since the cutover" in out


def test_a_log_line_whose_time_cannot_be_read_does_not_count(site, capsys):
    site["health"].write_text("not-a-time ALERT_SENT test - telegram\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL health: no delivered test alert since the cutover" in out


# ----------------------------------------------------------------------------------- sync


def test_the_sync_must_have_run_since_the_cutover(site, capsys):
    _ledger(site, _sync(BEFORE))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync: no sync run since the cutover" in out


def test_the_newest_sync_since_the_cutover_must_be_ok(site, capsys):
    _ledger(site, _sync(BEFORE), _sync(AFTER, ok=False))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert ("FAIL sync: the newest sync run since the cutover (2026-10-20T16:20:00Z): "
            "ok is false") in out.splitlines()

    # Only the newest run counts: a failed first attempt that a rerun made good does not.
    _ledger(site, _sync(BEFORE), _sync(AFTER_EARLY, ok=False), _sync(AFTER))
    rc, out = _run(site, capsys)
    assert rc == 0, out


def test_a_newest_run_that_is_not_ok_fails_even_when_the_first_run_carried_the_book_over(
        site, capsys):
    # The identity is judged on the first ok run since the cutover, and that one is fine here;
    # "the newest run is ok" is its own condition of the line, with its own message naming
    # the run (by the UTC time of its record, never by the record).
    _ledger(site, _sync(BEFORE), _sync(AFTER_EARLY), _sync(AFTER, ok=False))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the newest sync run since the cutover (2026-10-20T16:20:00Z): "
                              "ok is false")


def test_the_run_named_as_not_ok_is_named_in_utc_whatever_offset_its_record_carries(
        site, capsys):
    # 18:20+02:00 is 16:20Z, after the cutover; the name printed is the UTC time.
    _ledger(site, _sync(BEFORE), _sync(AFTER_EARLY), _sync("2026-10-20T18:20:00+02:00", ok=False))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the newest sync run since the cutover (2026-10-20T16:20:00Z): "
                              "ok is false")


def test_a_missing_sync_ledger_fails_the_sync_line(site, capsys):
    site["ledger"].unlink()
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "is missing" in out


def test_blank_lines_in_the_sync_ledger_are_skipped(site, capsys):
    _ledger(site, "", _sync(BEFORE), "", _sync(AFTER), "")
    rc, out = _run(site, capsys)
    assert rc == 0, out


def test_without_an_ok_run_before_the_cutover_there_is_no_book_to_compare(site, capsys):
    _ledger(site, _sync(BEFORE, ok=False), _sync(AFTER))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync: no earlier ok run to compare the book with" in out


def test_an_untouched_book_carries_over(site, capsys):
    rc, out = _run(site, capsys)
    assert rc == 0, out
    # The derivation of the identity is printed with the verdict, and nothing else is: no
    # count differs between the two runs, so there is no "counts that differ" clause.
    assert ("PASS sync: book carried over "
            "(examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled)") in out.splitlines()


def test_a_fill_in_the_zenbooks_last_run_does_not_fail_the_sync(site, capsys):
    # The counts are per-run transitions, not book state: a row the Zenbook's last run
    # concluded filled is not examined again, with no venue activity at all in between.
    _ledger(site,
            _sync(BEFORE, examined=3, filled=1, unchanged=2),
            _sync(AFTER, examined=2, filled=0, unchanged=2))
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert ("PASS sync: book carried over "
            "(examined 3 -> 2, expected 2 = 3 - 1 filled - 0 cancelled); "
            "counts that differ between the two runs: filled 1 -> 0") in out.splitlines()
    assert "venue activity" not in out  # the counts differ for more reasons than the venue


@pytest.mark.parametrize("filled, cancelled", [(0, 1), (1, 1), (2, 1)])
def test_rows_the_zenbooks_last_run_concluded_are_not_examined_again(
        site, capsys, filled, cancelled):
    open_rows = 3
    examined = open_rows + filled + cancelled  # what the Zenbook's last run looked at
    _ledger(site,
            _sync(BEFORE, examined=examined, filled=filled, cancelled=cancelled,
                  unchanged=open_rows),
            _sync(AFTER, examined=open_rows, unchanged=open_rows))
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert any(line.startswith(f"PASS sync: book carried over (examined {examined} -> 3, "
                               f"expected 3 = {examined} - {filled} filled - "
                               f"{cancelled} cancelled)")
               for line in out.splitlines()), out


def test_venue_activity_between_the_two_syncs_is_reported_not_failed(site, capsys):
    # The brief's former failing case: one of the three rows was cancelled at the venue.
    _ledger(site, _sync(BEFORE), _sync(AFTER, unchanged=2, cancelled=1))
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert ("PASS sync: book carried over "
            "(examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled); "
            "counts that differ between the two runs: cancelled 0 -> 1, unchanged 3 -> 2"
            ) in out.splitlines()


def test_every_count_that_may_differ_is_printed_with_both_values(site, capsys):
    _ledger(site,
            _sync(BEFORE, examined=5, filled=1, partially_filled=1, cancelled=0, unchanged=3),
            _sync(AFTER, examined=4, filled=2, partially_filled=0, cancelled=1, unchanged=1))
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert ("PASS sync: book carried over "
            "(examined 5 -> 4, expected 4 = 5 - 1 filled - 0 cancelled); "
            "counts that differ between the two runs: filled 1 -> 2, partially_filled 1 -> 0, "
            "cancelled 0 -> 1, unchanged 3 -> 1") in out.splitlines()


def test_a_book_that_lost_its_rows_fails_and_names_the_identity(site, capsys):
    _ledger(site,
            _sync(BEFORE, examined=3, filled=1, unchanged=2),
            _sync(AFTER, examined=0, unchanged=0))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the book did not carry over: "
                              "examined 3 -> 0, expected 2 = 3 - 1 filled - 0 cancelled")


def test_a_book_that_gained_rows_fails_and_names_the_identity(site, capsys):
    _ledger(site, _sync(BEFORE), _sync(AFTER, examined=4, unchanged=4))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the book did not carry over: "
                              "examined 3 -> 4, expected 3 = 3 - 0 filled - 0 cancelled")


def test_a_change_in_the_venue_orders_the_book_does_not_know_fails(site, capsys):
    _ledger(site, _sync(BEFORE), _sync(AFTER, unmatched_venue_orders=1))
    rc, out = _run(site, capsys)
    assert rc == 1
    # The derivation goes with the verdict whichever half of the identity broke; the half
    # that held reads as itself (3 -> 3 against an expected 3).
    _only_failed(out, "sync", "the book did not carry over: "
                              "examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled; "
                              "unmatched_venue_orders 0 -> 1")


def test_both_identities_are_named_when_both_break(site, capsys):
    _ledger(site, _sync(BEFORE), _sync(AFTER, examined=2, unmatched_venue_orders=1))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the book did not carry over: "
                              "examined 3 -> 2, expected 3 = 3 - 0 filled - 0 cancelled; "
                              "unmatched_venue_orders 0 -> 1")


def test_the_sync_is_judged_on_the_first_ok_run_after_and_the_last_ok_run_before(site, capsys):
    foreign = {"examined": 9, "unchanged": 9, "unmatched_venue_orders": 2}  # not our book
    _ledger(site,
            _sync(LONG_BEFORE, **foreign),                # an older ok run: another book
            _sync(MID_BEFORE, ok=False, **foreign),       # a failed run between the two ok runs
            _sync(BEFORE),                                # the last ok run before: the reference
            _sync(JUST_BEFORE, ok=False, **foreign),      # a failed run: never the reference
            _sync(AFTER_EARLY, ok=False, **foreign),      # a failed run since: never the first
            _sync(AFTER_MID),                             # the first ok run since: the same book
            _sync(AFTER, examined=7, unchanged=7))        # the newest run since: not judged
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert ("PASS sync: book carried over "
            "(examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled)") in out.splitlines()


def test_the_book_is_judged_against_the_first_ok_run_not_the_newest(site, capsys):
    # Ruling 20. The Spark's first sync concluded a venue fill, so its second sync examines
    # one row fewer than its first: judged against the Zenbook's last run, the newest run
    # breaks the identity (a false FAIL on every later run of the gate). The first run is
    # the one that has to carry the Zenbook's book over.
    _ledger(site,
            _sync(BEFORE),
            _sync(AFTER_EARLY, examined=3, filled=1, unchanged=2),
            _sync(AFTER, examined=2, filled=0, unchanged=2))
    rc, out = _run(site, capsys)
    assert rc == 0, out
    assert ("PASS sync: book carried over "
            "(examined 3 -> 3, expected 3 = 3 - 0 filled - 0 cancelled); "
            "counts that differ between the two runs: filled 0 -> 1, unchanged 3 -> 2"
            ) in out.splitlines()


def test_a_newest_run_that_looks_right_does_not_rescue_a_first_run_that_broke_the_book(
        site, capsys):
    # The other direction: a row appeared at the cutover (the first Spark run examined 4),
    # and a later run that examines 3 again must not hide it.
    _ledger(site,
            _sync(BEFORE),
            _sync(AFTER_EARLY, examined=4, unchanged=4),
            _sync(AFTER))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "the book did not carry over: "
                              "examined 3 -> 4, expected 3 = 3 - 0 filled - 0 cancelled")


def _ledger_with_a_broken_record(site, which: str, **changes) -> None:
    """The good BEFORE/AFTER ledger with one of the two lines changed.

    A change to None removes the field, so a missing count and a wrong one share a test.
    """
    record = json.loads(_sync(BEFORE if which == "before" else AFTER))
    record = {key: value for key, value in {**record, **changes}.items() if value is not None}
    lines = [json.dumps(record), _sync(AFTER)] if which == "before" else [
        _sync(BEFORE), json.dumps(record)]
    _ledger(site, *lines)


@pytest.mark.parametrize("which", ["before", "after"])
@pytest.mark.parametrize("name", ["examined", "filled", "partially_filled", "cancelled",
                                  "unchanged", "unmatched_venue_orders"])
def test_a_record_without_a_count_cannot_vouch_for_the_book(site, capsys, which, name):
    _ledger_with_a_broken_record(site, which, **{name: None})
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and f"has no whole-number {name}" in out


@pytest.mark.parametrize("bad", [True, 1.5, "3", [3]])
def test_a_count_that_is_not_a_whole_number_cannot_vouch_for_the_book(site, capsys, bad):
    _ledger_with_a_broken_record(site, "after", unmatched_venue_orders=bad)
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "has no whole-number unmatched_venue_orders" in out


def _torn(line: str) -> str:
    """A ledger line cut just after the account id: what a write that died half-way leaves."""
    torn = line[: line.index(ACCOUNT) + len(ACCOUNT) + 1]
    assert ACCOUNT in torn
    return torn


def test_a_partial_last_ledger_line_fails_the_sync_line_and_hides_nothing(site, capsys):
    site["ledger"].write_text(_sync(BEFORE) + "\n" + _torn(_sync(AFTER)), encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "could not be evaluated (JSONDecodeError at line 2)")


def test_a_torn_ledger_line_is_named_by_its_line_number_and_never_quoted(site, capsys):
    # The 1-based number of the line in the file, blank lines counted: line 4 here, the one
    # `sed -n 4p` prints. The lines after it are whole, and the torn one carries the account id.
    _ledger(site, "", _sync(BEFORE), "", _torn(_sync(AFTER_EARLY)), _sync(AFTER))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "could not be evaluated (JSONDecodeError at line 4)")


def test_a_torn_line_far_down_the_ledger_is_named_by_its_own_number(site, capsys):
    whole = [_sync(f"2026-10-{day:02d}T14:30:00+00:00") for day in range(1, 18)]  # 17 lines
    _ledger(site, *whole, _torn(_sync(AFTER)))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "could not be evaluated (JSONDecodeError at line 18)")


def test_a_ledger_record_without_ts_utc_fails_the_sync_line_and_hides_nothing(site, capsys):
    nameless = json.loads(_sync(AFTER))
    del nameless["ts_utc"]
    _ledger(site, _sync(BEFORE), json.dumps(nameless))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", "could not be evaluated (KeyError at line 2)")


@pytest.mark.parametrize("line, error", [
    (json.dumps({"ts_utc": "yesterday-ish", "account": ACCOUNT}), "ValueError"),
    (json.dumps({"ts_utc": 5, "account": ACCOUNT}), "TypeError"),
    (json.dumps([ACCOUNT, 1, 2]), "TypeError"),  # whole JSON, but not a record
])
def test_any_ledger_line_that_cannot_be_read_as_a_run_is_named_by_its_number(
        site, capsys, line, error):
    _ledger(site, _sync(BEFORE), line, _sync(AFTER))
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "sync", f"could not be evaluated ({error} at line 2)")


# --------------------------------------------------------------------------------- parity


def test_a_parity_mismatch_fails_the_gate_and_shows_every_reason(site, capsys):
    left, right = _scores(ZENBOOK), _scores(SPARK)
    right["x"][0] += 1.0
    _pair(site, left, right)
    rc, out = _run(site, capsys)
    assert rc == 1
    lines = out.splitlines()
    assert lines[3].startswith("FAIL parity:")
    assert any(line.startswith("    MISMATCH x[0]") for line in lines), out
    assert any(line.startswith("    PARITY FAILED: 1 mismatch(es)") for line in lines), out


def test_two_copies_of_one_machines_payload_are_not_parity(site, capsys):
    _pair(site, _scores(ZENBOOK), _scores(ZENBOOK))  # identical identity.machine blocks
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL parity" in out
    assert "    PARITY FAILED: identity.machine is identical on both sides" in out.splitlines()


def test_a_payload_without_a_machine_block_is_not_parity(site, capsys):
    _pair(site, _scores(None), _scores(SPARK))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "    PARITY FAILED: identity.machine is missing on left" in out.splitlines()


@pytest.mark.parametrize("leaves, passes", [(1000, True), (999, False)])
def test_the_payloads_must_compare_at_least_1000_numbers(site, capsys, leaves, passes):
    _pair(site, _scores(ZENBOOK, leaves), _scores(SPARK, leaves))
    rc, out = _run(site, capsys)
    assert (rc == 0) is passes, out
    if not passes:
        assert ("    PARITY FAILED: only 999 numeric leaf/leaves compared (floor 1000)"
                in out.splitlines())


@pytest.mark.parametrize("a, b, passes", [
    (100.0, 100.0 + 9e-7, True),     # 9e-9 relative: inside rtol 1e-8
    (100.0, 100.0 + 1.1e-6, False),  # 1.1e-8 relative: outside it
    (0.0, 9e-11, True),              # 9e-11 absolute: inside atol 1e-10
    (0.0, 1.1e-10, False),           # 1.1e-10 absolute: outside it
])
def test_the_registered_tolerance_is_rtol_1e8_and_atol_1e10(site, capsys, a, b, passes):
    _pair(site, _scores(ZENBOOK, probe=a), _scores(SPARK, probe=b))
    rc, out = _run(site, capsys)
    assert (rc == 0) is passes, out


@pytest.mark.parametrize("content, error", [(None, "FileNotFoundError"),
                                            ("{not json", "JSONDecodeError")])
def test_an_unreadable_parity_payload_fails_its_line_and_hides_nothing(
        site, capsys, content, error):
    if content is None:
        site["right"].unlink()
    else:
        site["right"].write_text(content, encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, "parity", f"could not be evaluated ({error})")


def test_without_the_parity_payloads_the_gate_cannot_pass(site, capsys):
    rc, out = _run(site, capsys, parity=False)
    assert rc == 1
    assert "FAIL parity" in out and "not checked" in out


# ------------------------------------------------------------- a line that cannot be judged


@pytest.mark.parametrize("name", ["sweep", "health", "sync", "parity"])
def test_every_line_goes_through_the_guard_and_names_only_the_exception_class(
        site, capsys, monkeypatch, name):
    def boom(*_args, **_kwargs):
        raise RuntimeError(f"text that must not be printed: {ACCOUNT}")

    monkeypatch.setattr(cutover_check, f"check_{name}", boom)
    rc, out = _run(site, capsys)
    assert rc == 1
    _only_failed(out, name, "could not be evaluated (RuntimeError)")
    assert "must not be printed" not in out


# ------------------------------------------------------------------------ --since, and the program


@pytest.mark.parametrize("since", ["2031-07-04 09:08:07", "2031-07-04T09:08:07", "2031-07-04",
                                   "tomorrow-ish"])
def test_a_since_without_a_utc_offset_is_a_usage_error_that_does_not_echo_it(
        site, capsys, since):
    argv = ["--root", str(site["root"]), "--repo", str(site["repo"]), "--since", since]
    with pytest.raises(SystemExit) as exited:
        cutover_check.main(argv, run=_systemd(0, AFTER))
    assert exited.value.code == 2
    seen = capsys.readouterr()
    _assert_quiet(seen.out, seen.err)
    assert seen.out == ""
    assert "--since" in seen.err and "UTC offset" in seen.err
    assert since not in seen.err


@pytest.mark.parametrize("since, passes", [
    ("2026-10-20T16:10:00Z", True),           # the good cutover's first file is at 16:15Z
    ("2026-10-20T18:10:00+02:00", True),      # 16:10Z; read as 18:10Z it would be too late
    ("2026-10-20T11:10:00-05:00", True),
    ("2026-10-20T16:25:00Z", False),          # after all of it
    ("2026-10-20T18:25:00+02:00", False),
    ("2026-10-20T11:25:00-05:00", False),     # 16:25Z; read as 11:25Z it would be early
])
def test_a_since_is_read_with_its_own_utc_offset(site, capsys, since, passes):
    rc, out = _run(site, capsys, since=since)
    assert (rc == 0) is passes, out


def test_what_happened_at_the_very_second_of_the_cutover_counts(site, capsys):
    site["sweep"].unlink()
    _write_sweep(site["root"], SINCE)
    # The run at the very second is the one "after": it is judged against the run before it,
    # not against itself (which would not hold: it examined 2 rows after concluding 1 fill).
    _ledger(site,
            _sync(BEFORE, examined=3, filled=1, unchanged=2),
            _sync(SINCE, examined=2, filled=1, unchanged=1))
    site["health"].write_text(f"{SINCE} ALERT_SENT test - telegram\n", encoding="utf-8")
    rc, out = _run(site, capsys, systemd=_systemd(0, SINCE))
    assert rc == 0, out


def test_what_happened_a_second_before_the_cutover_does_not_count(site, capsys):
    site["sweep"].unlink()
    _write_sweep(site["root"], JUST_BEFORE)
    _ledger(site, _sync(BEFORE), _sync(JUST_BEFORE))
    site["health"].write_text(f"{JUST_BEFORE} ALERT_SENT test - telegram\n", encoding="utf-8")
    rc, out = _run(site, capsys, systemd=_systemd(0, JUST_BEFORE))
    assert rc == 1
    lines = out.splitlines()
    assert "FAIL sweep: no sweep file written since the cutover" in lines
    assert any(line.startswith("FAIL health:") and "has not finished a run" in line
               and "no delivered test alert" in line for line in lines)
    assert "FAIL sync: no sync run since the cutover" in lines


def test_the_script_run_as_a_program_exits_2_on_a_since_without_an_offset(tmp_path):
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "spark" / "cutover_check.py"),
         "--root", str(tmp_path), "--repo", str(tmp_path), "--since", "2031-07-04 09:08:07"],
        capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert done.returncode == 2
    _assert_quiet(done.stdout, done.stderr)
    assert done.stdout == ""
    assert "UTC offset" in done.stderr
    assert "2031-07-04" not in done.stderr
