"""Evaluate the cutover gate from what the runtime leaves behind.

    python scripts/spark/cutover_check.py --root /srv/quant_data --repo /home/trading/forecast-model \\
        --since 2026-10-20T16:00:00+00:00 --parity zenbook.json spark.json

Four lines, every one of which must hold (spec section 6, step 5), all judged
on what happened at or after --since, the moment the Spark's units started:

  sweep   a sweep file with at least MIN_SWEEP_ROWS rows was written;
  health  the health unit finished a run with exit status 0, and a test alert
          was delivered by Telegram;
  sync    the paper sync ran, is ok, and reports the same book as the last ok
          run before --since (the Zenbook's);
  parity  the two score payloads pass the registered parity gate.

The probe writes to logs/ibkr_health.log only when it finds a problem, so a
healthy run is read from systemd (the unit's last exit status and exit time),
not from that log. Run it on the Spark. It reads; it runs nothing against the
venue and sends nothing. Exit 0 when all four pass, 1 otherwise.
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

MIN_SWEEP_ROWS = 2000
HEALTH_UNIT = "ibkr-health.service"
PARITY_FLAGS = ["--rtol", "1e-8", "--atol", "1e-10", "--min-numeric", "1000",
                "--ignore", "identity.machine", "--differ", "identity.machine"]
SYNC_COUNTS = ("examined", "filled", "partially_filled", "cancelled", "unchanged",
               "unmatched_venue_orders")


def _when(text: str) -> datetime:
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def check_sweep(root: Path, since: datetime) -> tuple[bool, str]:
    files = sorted((root / "ibkr_ticks" / "quotes").glob("date=*/*.parquet"),
                   key=lambda p: p.stat().st_mtime)
    fresh = [p for p in files
             if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since]
    if not fresh:
        return False, "no sweep file written since the cutover"
    rows = pq.read_metadata(fresh[-1]).num_rows
    if rows < MIN_SWEEP_ROWS:
        return False, f"{fresh[-1].name} has {rows} rows, under {MIN_SWEEP_ROWS}"
    return True, f"{fresh[-1].name}, {rows} rows"


def _health_unit_last_run(run) -> tuple[int | None, datetime | None]:
    """(exit status, exit time) of the health unit's last run; the time is None if it never ran."""
    done = run(["systemctl", "show", HEALTH_UNIT, "--timestamp=unix",
                "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"],
               capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise OSError(f"systemctl exit {done.returncode}")
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


def check_sync(root: Path, since: datetime) -> tuple[bool, str]:
    ledger = root / "ibkr_ticks" / "paper" / "ibkr_sync_runs.jsonl"
    if not ledger.is_file():
        return False, f"{ledger} is missing"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    after = [r for r in records if _when(r["ts_utc"]) >= since]
    before_ok = [r for r in records if _when(r["ts_utc"]) < since and r.get("ok")]
    if not after:
        return False, "no sync run since the cutover"
    last = after[-1]
    if not last.get("ok"):
        return False, "the last sync run's ok is false"
    if not before_ok:
        return False, "no earlier ok run to compare the book with"
    reference = before_ok[-1]
    differing = [name for name in SYNC_COUNTS if last.get(name) != reference.get(name)]
    if differing:
        detail = ", ".join(f"{name} {reference.get(name)} -> {last.get(name)}"
                           for name in differing)
        return False, f"the book differs from the last run before the cutover: {detail}"
    return True, "ok, same counts as the last run before the cutover"


def check_parity(payloads: list[str] | None) -> tuple[bool, str]:
    if not payloads:
        return False, "not checked: pass --parity ZENBOOK.json SPARK.json"
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = compare_json.main([*payloads, *PARITY_FLAGS])
    lines = captured.getvalue().strip().splitlines()
    return code == 0, lines[-1] if lines else f"compare_json exit {code}"


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the live quant-data root")
    parser.add_argument("--repo", required=True, help="the runtime clone (its logs/ is read)")
    parser.add_argument("--since", required=True,
                        help="ISO time the Spark's units were started, with its UTC offset")
    parser.add_argument("--parity", nargs=2, metavar=("ZENBOOK_JSON", "SPARK_JSON"), default=None)
    args = parser.parse_args(argv)
    since = _when(args.since)
    root, repo = Path(args.root).expanduser(), Path(args.repo).expanduser()

    results = [("sweep", *check_sweep(root, since)), ("health", *check_health(repo, since, run)),
               ("sync", *check_sync(root, since)), ("parity", *check_parity(args.parity))]
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
    verdict = all(passed for _name, passed, _detail in results)
    print("CUTOVER GATE: " + ("PASS" if verdict else "FAIL -- roll back"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
