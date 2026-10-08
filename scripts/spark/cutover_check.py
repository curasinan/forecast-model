"""Evaluate the cutover gate from what the runtime leaves behind.

    python scripts/spark/cutover_check.py --root /srv/quant_data \\
        --repo /home/trading/forecast-model --since 2026-10-20T16:00:00+00:00 \\
        --parity zenbook.json spark.json

Four lines, every one of which must hold (spec section 6, step 5), all judged
on what happened at or after --since, the moment the Spark's units started:

  sweep   a sweep file with at least MIN_SWEEP_ROWS rows was written: the newest
          sweep taken since --since is the one judged, and a sweep's time is the
          UTC time its own path carries (date=YYYY-MM-DD/quotes-HHMMSSZ.parquet),
          the file's mtime only when the name carries none;
  health  the health unit finished a run with exit status 0, and a test alert
          was delivered by Telegram;
  sync    the paper sync ran since --since, its newest run since then is ok, and
          the book carried over from the last ok run before --since (the
          Zenbook's) to the first ok run since --since (the Spark's first). A
          run's counts are what that run concluded, not the state of the book:
          a row concluded filled or cancelled is no longer examined, so the book
          carried over when
              examined(after) == examined(before) - filled(before) - cancelled(before)
          and unmatched_venue_orders(after) == unmatched_venue_orders(before).
          Only the first ok run is held to that: a later run examines one row
          fewer for every row an earlier Spark run concluded, and that is not the
          book breaking. filled, partially_filled, cancelled and unchanged may
          differ between the two runs; the line prints each one that did, and the
          derivation of the examined count it expected (passing or failing), and
          ends with the two runs it compared, by their UTC times, and the number of
          not-ok runs between them: a not-ok Spark run that concluded rows between the
          two breaks the identity without the book being broken, and that is how such a
          FAIL is told from a broken book;
  parity  the two score payloads pass the registered parity gate; compare_json's
          own output follows the verdict line, every line indented four spaces.

The probe writes to logs/ibkr_health.log only when it finds a problem, so a
healthy run is read from systemd (the unit's last exit status and exit time),
not from that log. Run it on the Spark. It reads; it runs nothing against the
venue and sends nothing. Exit 0 when all four pass, 1 otherwise.

Each line is judged on its own: one that cannot be evaluated (an unreadable file,
a torn ledger line, a missing payload) prints "FAIL <line>: could not be evaluated
(<ExceptionClass>)" and the other three verdicts still print. Only the class name
is printed, never the exception text, which can quote the data that broke it. The
sync ledger's lines carry the paper account id; no line of it is ever printed. A
ledger line that cannot be read says where it is, "(JSONDecodeError at line 17)":
the 1-based number of the line in the file, blank lines counted.
--since must carry its UTC offset; anything else is a usage error (exit 2).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compare_json  # noqa: E402
from climate.ladder import sweep_ts_from_path  # noqa: E402  (compare_json put the repo on sys.path)

MIN_SWEEP_ROWS = 2000
HEALTH_UNIT = "ibkr-health.service"
PARITY_FLAGS = ["--rtol", "1e-8", "--atol", "1e-10", "--min-numeric", "1000",
                "--ignore", "identity.machine", "--differ", "identity.machine"]
SYNC_COUNTS = ("examined", "filled", "partially_filled", "cancelled", "unchanged",
               "unmatched_venue_orders")
# The counts that may differ between the Zenbook's last ok run and the Spark's first.
ACTIVITY_COUNTS = ("filled", "partially_filled", "cancelled", "unchanged")
SINCE_USAGE = "must be an ISO-8601 time with its UTC offset, for example 2026-10-20T16:00:00+00:00"


def _when(text: str) -> datetime:
    """A time from the runtime's own files, which write UTC; one with no offset reads as UTC."""
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _since(text: str) -> datetime:
    """The --since value: an ISO-8601 time that carries its UTC offset.

    A bad value is rejected with a fixed message that does not repeat it.
    """
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(SINCE_USAGE) from None
    if stamp.utcoffset() is None:
        raise argparse.ArgumentTypeError(SINCE_USAGE)
    return stamp


def _sweep_time(path: Path) -> datetime:
    """When a sweep was taken: the UTC time its own path carries, else the file's mtime.

    The recorder names a sweep for the moment it wrote it. The mtime only says when this
    copy of the file was made, which for a root the Zenbook's files were copied into is the
    time of the copy, not of the sweep.
    """
    try:
        named = sweep_ts_from_path(path)
    except ValueError:  # the recorder's shape, but not a real date or time
        named = None
    return named or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def check_sweep(root: Path, since: datetime) -> tuple[bool, str]:
    quotes = root / "ibkr_ticks" / "quotes"
    taken = sorted((_sweep_time(path), path) for path in quotes.glob("date=*/*.parquet"))
    fresh = [path for when, path in taken if when >= since]
    if not fresh:
        return False, "no sweep file written since the cutover"
    newest = fresh[-1]
    rows = pq.read_metadata(newest).num_rows
    if rows < MIN_SWEEP_ROWS:
        return False, f"{newest.name} has {rows} rows, under {MIN_SWEEP_ROWS}"
    return True, f"{newest.name}, {rows} rows"


def _health_unit_last_run(run) -> tuple[int | None, datetime | None]:
    """(exit status, exit time) of the health unit's last run; the time is None if it never ran."""
    done = run(["systemctl", "show", HEALTH_UNIT, "--timestamp=unix",
                "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"],
               capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()[:1]  # systemctl's own words, one line
        raise OSError(f"systemctl exit {done.returncode}" + (f": {first[0]}" if first else ""))
    fields = dict(line.split("=", 1) for line in (done.stdout or "").splitlines() if "=" in line)
    raw = fields.get("ExecMainExitTimestamp", "").strip()
    if raw in ("", "n/a"):
        exited = None
    else:
        stamp = re.fullmatch(r"@(\d+)", raw)
        if not stamp:
            raise ValueError(f"unreadable exit time {raw!r}")
        exited = datetime.fromtimestamp(int(stamp.group(1)), tz=timezone.utc)
    status = fields.get("ExecMainStatus", "").strip()
    return (int(status) if status.lstrip("-").isdigit() else None), exited


def _health_log_since(repo: Path, since: datetime) -> tuple[str | None, bool]:
    """(last non-OK status line, whether a test alert was delivered by Telegram), since."""
    log = repo / "logs" / "ibkr_health.log"
    last_problem, test_alert = None, False
    if not log.is_file():
        return last_problem, test_alert
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            if _when(parts[0]) < since:
                continue
        except ValueError:
            continue
        if parts[1] == "ALERT_SENT":
            if parts[2:5] == ["test", "-", "telegram"]:
                test_alert = True
        elif not parts[1].startswith("ALERT_"):
            last_problem = " ".join(parts[1:])
    return last_problem, test_alert


def check_health(repo: Path, since: datetime, run=subprocess.run) -> tuple[bool, str]:
    last_problem, test_alert = _health_log_since(repo, since)
    problems = []
    exited = None
    try:
        status, exited = _health_unit_last_run(run)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        problems.append(f"could not ask systemd about {HEALTH_UNIT} ({type(exc).__name__}: {exc})")
    else:
        if exited is None or exited < since:
            problems.append(f"{HEALTH_UNIT} has not finished a run since the cutover")
        elif status != 0:
            detail = f": {last_problem}" if last_problem else ""
            problems.append(f"the probe's last exit status is {status}{detail}")
    if not test_alert:
        problems.append("no delivered test alert since the cutover")
    if problems:
        return False, "; ".join(problems)
    return True, f"probe exit 0 at {exited:%H:%M:%S}Z, test alert delivered by telegram"


def _counts(record: dict, which: str) -> dict[str, int] | str:
    """The six counts of a ledger record, or what is wrong with it (never a ledger line)."""
    counts = {name: record.get(name) for name in SYNC_COUNTS}
    missing = [name for name, value in counts.items() if type(value) is not int]
    if missing:
        return f"{which} has no whole-number {', '.join(missing)}"
    return counts


def _carry_over(before: dict[str, int], after: dict[str, int]) -> tuple[bool, str]:
    """Did the book carry over from the run `before` to the run `after`?

    The run before concluded `filled` and `cancelled` rows, which left the set a sync
    examines, so the run after examines exactly the rest; and the venue orders the book
    does not know are as many as before. The other counts may differ. The verdict, passing
    or failing, prints the derivation of the examined count it expected.
    """
    expected = before["examined"] - before["filled"] - before["cancelled"]
    examined = (f"examined {before['examined']} -> {after['examined']}, expected {expected} = "
                f"{before['examined']} - {before['filled']} filled - "
                f"{before['cancelled']} cancelled")
    unmatched_moved = after["unmatched_venue_orders"] != before["unmatched_venue_orders"]
    if after["examined"] != expected or unmatched_moved:
        broken = [examined]  # the half that held reads as itself: 3 -> 3 against an expected 3
        if unmatched_moved:
            broken.append(f"unmatched_venue_orders {before['unmatched_venue_orders']} -> "
                          f"{after['unmatched_venue_orders']}")
        return False, "the book did not carry over: " + "; ".join(broken)
    detail = f"book carried over ({examined})"
    moved = [f"{name} {before[name]} -> {after[name]}"
             for name in ACTIVITY_COUNTS if after[name] != before[name]]
    if moved:
        detail += "; counts that differ between the two runs: " + ", ".join(moved)
    return True, detail


def _stamp(when: datetime) -> str:
    """A time as the gate prints it: UTC, to the second, with a trailing Z."""
    return f"{when.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def _compared(runs: list[tuple[datetime, dict]], before: int, after: int) -> str:
    """The two ledger runs (by position in `runs`) a carry-over verdict compares: their UTC
    times and how many not-ok runs lie between them in the ledger.

    A not-ok Spark run that concluded rows between the two breaks the identity without the
    book being broken; this tells such a FAIL from a broken book. Two times and a count are
    all that is printed of the ledger: a record carries the paper account id.
    """
    not_ok = sum(1 for _, run in runs[before + 1:after] if not run.get("ok"))
    return (f"(before {_stamp(runs[before][0])}, after {_stamp(runs[after][0])}, "
            f"{not_ok} not-ok {'run' if not_ok == 1 else 'runs'} between)")


def _unevaluated(what: str) -> str:
    """The detail of a line that could not be judged; `what` is a class name, never data."""
    return f"could not be evaluated ({what})"


def _ledger_runs(ledger: Path) -> list[tuple[datetime, dict]] | str:
    """Every run in the sync ledger, oldest first, each with its time; or what cannot be read.

    The ledger is append-only, so file order is time order. A line that cannot be read as a
    run (torn, not a record, or with no usable ts_utc) is named by its 1-based number in the
    file, blank lines counted, and the exception's class: never by its content, which
    carries the paper account id.
    """
    runs = []
    for number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            runs.append((_when(record["ts_utc"]), record))
        except (ValueError, KeyError, TypeError) as exc:
            return _unevaluated(f"{type(exc).__name__} at line {number}")
    return runs


def check_sync(root: Path, since: datetime) -> tuple[bool, str]:
    ledger = root / "ibkr_ticks" / "paper" / "ibkr_sync_runs.jsonl"
    if not ledger.is_file():
        return False, f"{ledger} is missing"
    runs = _ledger_runs(ledger)
    if isinstance(runs, str):
        return False, runs
    # Positions in the ledger, which is in time order: the compared runs are named by them.
    after = [number for number, (when, _) in enumerate(runs) if when >= since]
    before_ok = [number for number, (when, run) in enumerate(runs)
                 if when < since and run.get("ok")]
    if not after:
        return False, "no sync run since the cutover"
    # Two conditions. The newest run since the cutover must be ok ...
    newest_when, newest = runs[after[-1]]
    if not newest.get("ok"):
        return False, f"the newest sync run since the cutover ({_stamp(newest_when)}): ok is false"
    if not before_ok:
        return False, "no earlier ok run to compare the book with"
    # ... and the book is judged on the FIRST ok run since: it is the one that has to carry
    # the Zenbook's book over. Every later run examines one row fewer per row an earlier
    # Spark run concluded, so none of them is held to the identity against the Zenbook's run.
    before = before_ok[-1]
    first_ok = next(number for number in after if runs[number][1].get("ok"))  # the newest is ok
    counts = []
    for number, which in ((before, "the last ok sync run before the cutover"),
                          (first_ok, "the first ok sync run since the cutover")):
        found = _counts(runs[number][1], which)
        if isinstance(found, str):
            return False, found
        counts.append(found)
    passed, detail = _carry_over(*counts)
    return passed, f"{detail} {_compared(runs, before, first_ok)}"


def check_parity(payloads: list[str] | None) -> tuple[bool, str]:
    if not payloads:
        return False, "not checked: pass --parity ZENBOOK.json SPARK.json"
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = compare_json.main([*payloads, *PARITY_FLAGS])
    # Every line compare_json printed goes under the verdict, indented: the leaf count and
    # the machines compared when it passes, every mismatch and reason when it does not.
    shown = "".join(f"\n    {line}" for line in captured.getvalue().splitlines())
    return code == 0, f"compare_json exit {code}{shown}"


def _verdict(evaluate) -> tuple[bool, str]:
    """One line's verdict. A check that cannot be evaluated is that line's FAIL, not a crash.

    Only the exception's class name is printed: its text can quote the data that broke it
    (a torn ledger line carries the paper account id).
    """
    try:
        return evaluate()
    except Exception as exc:  # noqa: BLE001 -- any failure to judge a line IS that line's FAIL
        return False, _unevaluated(type(exc).__name__)


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the live quant-data root")
    parser.add_argument("--repo", required=True, help="the runtime clone (its logs/ is read)")
    parser.add_argument("--since", required=True, type=_since,
                        help="ISO-8601 time the Spark's units were started, with its UTC offset")
    parser.add_argument("--parity", nargs=2, metavar=("ZENBOOK_JSON", "SPARK_JSON"), default=None)
    args = parser.parse_args(argv)
    since = args.since
    root, repo = Path(args.root).expanduser(), Path(args.repo).expanduser()

    lines = (("sweep", lambda: check_sweep(root, since)),
             ("health", lambda: check_health(repo, since, run)),
             ("sync", lambda: check_sync(root, since)),
             ("parity", lambda: check_parity(args.parity)))
    verdict = True
    for name, evaluate in lines:
        passed, detail = _verdict(evaluate)
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
        verdict = verdict and passed
    print("CUTOVER GATE: " + ("PASS" if verdict else "FAIL -- roll back"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
