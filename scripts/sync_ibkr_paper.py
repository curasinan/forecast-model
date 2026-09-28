"""IBKR PAPER reconciliation entrypoint (Task 1.6).

A GTC paper order rests at ForecastEx for days or weeks. Nothing placing an
order stays alive that long, so a fill that lands after the placing process
exits was never recorded anywhere -- the book could not state its own
results. This script is what a daily Task Scheduler job runs to close that
hole: it asks the venue what actually happened to every resting order and
writes the consequences into `ibkr_paper.db` via
`src.trading.reconcile.reconcile_paper_book`.

It will FIRST be run by the account holder by hand, never by an agent -- but
every safety property below is enforced in code, not by convention, because
this is also exactly the script Task Scheduler will run unattended forever
after that.

Safety properties:
- Refuses to start unless IBKR_TRADING_MODE=paper (settings) AND the
  connected account is a paper account (DU/DF prefix, verified by the
  adapter's connect()). A live account never gets past connect().
- The adapter is constructed with role="paper_sync", read_only=True.
  Read-only means `place_order`/`cancel_order` raise `VenueReadOnlyError`
  (src/venues/base.py) -- this script is structurally incapable of sending
  an order to the venue. Keep it that way; nothing here should ever change
  `read_only` to False.
- A sidecar lockfile (`src/trading/locks.py`) around the whole reconcile
  call excludes a concurrent writer of the same `ibkr_paper.db` -- an
  operator's manual run racing the scheduled job, or two overlapping
  scheduled runs.
- `reconcile_paper_book`'s own mass-cancellation guard (controller ruling
  R8) can abort a run rather than risk inventing cancellations from a
  degraded Gateway answer; `--allow-mass-cancel` disables that guard for an
  operator who has confirmed the cancellations at the venue. An aborted run
  is reported and treated as unhealthy (exit 1, `ok: false` in the run
  ledger) -- it is not a crash, but it is not success either.
- A candidate book row with no permId on file (final-review finding I-1)
  can never be joined to venue state, so its fills are invisible. Such a
  run still reconciles every other row, but is ALSO reported unhealthy
  (exit 1, `ok: false`, `skipped_missing_perm_id` > 0) so the health
  probe's SYNC_STALE axis fires instead of the gap staying silent.

Run ledger: every invocation appends exactly one JSON line to
`<db_path's data root>/paper/ibkr_sync_runs.jsonl`. Task 2.2's health probe
keys on the `ok` field, which is true only when the run completed, the
mass-cancellation guard did not fire, AND no candidate row was skipped for
a missing permId (`report_is_healthy`). An unhandled exception during
reconciliation still gets a best-effort line with `ok: false` before this
process exits 1, so the health probe sees a failure instead of just a gap.

Usage:
    set IBKR_PORT=4002  IBKR_TRADING_MODE=paper   (Gateway logged into PAPER)
    python scripts/sync_ibkr_paper.py
    python scripts/sync_ibkr_paper.py --db D:\\quant_data\\paper\\ibkr_paper.db
    python scripts/sync_ibkr_paper.py --allow-mass-cancel   # after venue confirmation
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.trading.reconcile import ReconcileReport, reconcile_paper_book  # noqa: E402
from src.venues.ibkr.adapter import IBKRAdapter  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("sync_ibkr_paper")

RUNS_FILENAME = "ibkr_sync_runs.jsonl"


# ---------------------------------------------------------------- pure helpers
def report_is_healthy(report: ReconcileReport) -> bool:
    """True only for a run that completed without the mass-cancellation
    guard firing AND left no candidate row outside fill capture.

    Final-review finding I-1: a candidate row with no permId on file is
    skipped by `reconcile_paper_book` on every run. Before this check the
    run still recorded `ok: true`, so an order whose permId was not yet
    assigned at ack time dropped out of fill capture with nothing ever
    alerting. Choice: report such a run as `ok: false` (and exit 1) rather
    than inventing a new ledger field the health probe would have to learn
    -- `ok: false` lines never refresh the probe's last-successful-sync
    timestamp, so the EXISTING SYNC_STALE axis
    (`scripts/check_ibkr_recorder.py`) fires once the gap outlives its
    threshold, and keeps firing until the row is backfilled. The
    reconciliation itself still ran and wrote every other conclusion; only
    the health verdict changes.
    """
    return not report.aborted and report.skipped_missing_perm_id == 0


def build_jsonl_record(report: ReconcileReport, *, account: str, db: str,
                       ok: bool | None = None, ts: datetime | None = None) -> dict:
    """One run-ledger record from a `ReconcileReport` (Task 2.2's health probe
    keys on `ok`).

    `ok` defaults to :func:`report_is_healthy` -- a run that completed
    without the mass-cancellation guard firing and with no row skipped for
    a missing permId. Pass `ok=False` explicitly for the best-effort record
    written when reconciliation raised: `report` is then a placeholder
    (typically `ReconcileReport(notes=[...])`) carrying only what is known,
    and the caller states the outcome rather than letting the placeholder's
    default `aborted=False` imply success. An explicit `ok` can only ever
    make the verdict WORSE: `ok=True` never masks an unhealthy report.
    """
    stamp = ts or datetime.now(timezone.utc)
    healthy = report_is_healthy(report)
    return {
        "ts_utc": stamp.isoformat(),
        "ok": healthy if ok is None else (bool(ok) and healthy),
        "aborted": report.aborted,
        "examined": report.examined,
        "filled": report.filled,
        "partially_filled": report.partially_filled,
        "cancelled": report.cancelled,
        "unchanged": report.unchanged,
        "unmatched_venue_orders": report.unmatched_venue_orders,
        "skipped_missing_perm_id": report.skipped_missing_perm_id,
        "notes": list(report.notes),
        "account": account,
        "db": db,
    }


def format_report(report: ReconcileReport) -> str:
    """Human-readable summary: the counters on one line, then every note."""
    lines = [
        f"examined={report.examined} filled={report.filled} "
        f"partially_filled={report.partially_filled} cancelled={report.cancelled} "
        f"unchanged={report.unchanged} unmatched_venue_orders={report.unmatched_venue_orders} "
        f"skipped_missing_perm_id={report.skipped_missing_perm_id} "
        f"aborted={report.aborted}"
    ]
    for note in report.notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile the IBKR paper book against venue truth (paper only)"
    )
    ap.add_argument("--db", default=None,
                    help="paper position DB (default <IBKR data root>/paper/ibkr_paper.db)")
    ap.add_argument("--allow-mass-cancel", action="store_true",
                    help="disable the mass-cancellation guard (controller ruling R8); "
                         "only after confirming the cancellations at the venue")
    ap.add_argument("--stale-lock-min", type=float, default=10.0,
                    help="a lockfile older than this many minutes is broken (default 10)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = IBKRSettings()  # raises if IBKR_PORT / IBKR_TRADING_MODE are missing
    if settings.trading_mode != "paper":
        print("refusing: this is the PAPER sync entrypoint; IBKR_TRADING_MODE must be 'paper'",
              file=sys.stderr)
        return 2

    adapter = IBKRAdapter(settings, role="paper_sync", read_only=True)
    adapter.connect()  # ModeMismatchError unless the account is DU/DF (paper)
    if adapter.mode != "paper":  # belt and braces
        print(f"refusing: connected account {adapter.account} is not paper", file=sys.stderr)
        return 2
    print(f"account={adapter.account} mode={adapter.mode} role={adapter.role} "
          f"read_only={adapter.read_only}")

    db_path = Path(args.db) if args.db else settings.resolved_data_root / "paper" / "ibkr_paper.db"
    runs_path = db_path.parent / RUNS_FILENAME

    if not db_path.exists():
        print(f"no paper book at {db_path}; run "
              "scripts/migrations/migrate_ibkr_paper_v2.py first, or check --db / the "
              "IBKR data root", file=sys.stderr)
        adapter.disconnect()
        return 1

    try:
        with acquire_db_lock(db_path, stale_after_min=args.stale_lock_min):
            try:
                report = reconcile_paper_book(str(db_path), adapter,
                                              allow_mass_cancel=args.allow_mass_cancel)
            except Exception as exc:  # noqa: BLE001 -- record it, then let exit 1 say so
                log.exception("reconcile failed")
                error_report = ReconcileReport(notes=[f"EXCEPTION: {exc}"])
                _append_jsonl(runs_path, build_jsonl_record(
                    error_report, account=adapter.account or "", db=str(db_path), ok=False,
                ))
                print(f"reconcile failed: {exc}", file=sys.stderr)
                adapter.disconnect()
                return 1
    except LockHeldError as exc:
        # Another run is active: no jsonl append -- this process never held
        # the lock, so it observed nothing worth recording.
        print(f"refusing: {exc}", file=sys.stderr)
        adapter.disconnect()
        return 1

    print(format_report(report))
    record = build_jsonl_record(report, account=adapter.account or "", db=str(db_path))
    _append_jsonl(runs_path, record)
    if report.skipped_missing_perm_id:
        print(f"UNHEALTHY: {report.skipped_missing_perm_id} book row(s) have no perm_id on "
              "file and are outside fill capture -- backfill with "
              "scripts/migrations/migrate_ibkr_paper_v2.py", file=sys.stderr)
    adapter.disconnect()
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
