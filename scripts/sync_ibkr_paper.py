"""IBKR PAPER reconciliation entrypoint."""

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
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
from src.venues.ibkr.adapter import IBKRAdapter  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("sync_ibkr_paper")

RUNS_FILENAME = "ibkr_sync_runs.jsonl"


def report_is_healthy(report: ReconcileReport) -> bool:
    """True only for a complete run with no guard trip and no row outside fill capture."""
    return not report.aborted and report.skipped_missing_perm_id == 0


def build_jsonl_record(report: ReconcileReport, *, account: str, db: str,
                       ok: bool | None = None, ts: datetime | None = None) -> dict:
    """One run-ledger record from a `ReconcileReport` (the health probe keys on `ok`)."""
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile the IBKR paper book against venue truth (paper only)"
    )
    ap.add_argument("--db", default=None,
                    help="paper position DB (default <IBKR data root>/paper/ibkr_paper.db)")
    ap.add_argument("--allow-mass-cancel", action="store_true",
                    help="disable the mass-cancellation guard; "
                         "only after confirming the cancellations at the venue")
    ap.add_argument("--stale-lock-min", type=float, default=10.0,
                    help="a lockfile older than this many minutes is broken (default 10)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = IBKRSettings()
    if settings.trading_mode != "paper":
        print("refusing: this is the PAPER sync entrypoint; IBKR_TRADING_MODE must be 'paper'",
              file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else settings.resolved_data_root / "paper" / "ibkr_paper.db"
    try:
        refuse_if_mirror(db_path.parent, "reconcile the paper book")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    adapter = IBKRAdapter(settings, role="paper_sync", read_only=True)
    adapter.connect()
    if adapter.mode != "paper":  # belt and braces
        print(f"refusing: connected account {adapter.account} is not paper", file=sys.stderr)
        return 2
    print(f"account={adapter.account} mode={adapter.mode} role={adapter.role} "
          f"read_only={adapter.read_only}")

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
