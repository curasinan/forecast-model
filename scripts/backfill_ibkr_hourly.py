"""Backfill hourly bars for days the sweep recorder missed (Gateway logged out).

Uses reqHistoricalData through the pacing limiter and writes into a SEPARATE
tree from the live sweeps (``quotes_hourly/date=.../``) -- these are hourly
OHLC aggregates with different semantics, not point-in-time snapshots. See
src/venues/ibkr/backfill.py for the provenance and BID_ASK field mapping.

Read-only: the connected session only issues historical-data requests. The
account mode is cross-checked against IBKR_TRADING_MODE exactly as the
recorder does, so bars never get filed under the wrong session label.

Usage (Gateway logged in, e.g. paper on 127.0.0.1:4002):
    python scripts/backfill_ibkr_hourly.py --dry-run       # plan only, no venue
    python scripts/backfill_ibkr_hourly.py                 # climate lane, BID_ASK
    python scripts/backfill_ibkr_hourly.py --symbols GTM --what BID_ASK MIDPOINT
    python scripts/backfill_ibkr_hourly.py --all-symbols --max-requests 400
    python scripts/backfill_ibkr_hourly.py --start 2026-09-03 --end 2026-09-03 \
        --refetch 2026-09-03                               # finish a partly filled day

A day that already holds an hourly file counts as done and is skipped. Name it
with --refetch to fetch it again: bars already stored are not written twice, so
that is how a cut run is finished (every contract is requested again over the
window, about two hours for the climate lane) and how a day holding only part
of its bars is completed. One request per contract spans the whole window, so
days inside it that hold sweeps only get hourly bars as well; the dry run
lists them.

Exit codes: 0 done (bars written or nothing missing), 2 the window or a
--refetch date is invalid, 3 Gateway offline / connection refused, 4 nothing
to request (no contracts matched, or the gap is longer than one request can
fill), 5 a stored hourly file cannot be read, 6 another run holds the lock,
130 interrupted (what was fetched is kept and recorded).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.venues.ibkr.backfill import (  # noqa: E402
    HISTORY_STARTS,
    MAX_GAP_DAYS,
    HistoricalBackfiller,
    missing_dates,
    plan_requests,
    read_stored_window,
)
from src.venues.ibkr.contracts import ContractResolver  # noqa: E402
from src.venues.ibkr.gateway import GatewaySession  # noqa: E402
from src.venues.ibkr.recorder import setup_logging  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("backfill_ibkr_hourly")

# The modelled lane (docs/gtm_model_report.md) plus the other climate symbols
# whose ladders the GTM work reads; everything else needs --symbols/--all-symbols.
CLIMATE_LANE = ("GTM", "GT", "GTTA", "GTTM", "ACD")
SNAPSHOT = REPO_ROOT / "data" / "ibkr_universe_live.json"
# One run at a time per store: two overlapping runs would each read the stored
# bars once and then both write the same ones. A run is about two hours for
# the climate lane, so the lock only counts as abandoned well after that.
LOCK_STALE_MIN = 240.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", nargs="+", default=list(CLIMATE_LANE),
                   help=f"underlying symbols to backfill (default: {' '.join(CLIMATE_LANE)})")
    p.add_argument("--all-symbols", action="store_true",
                   help="backfill every tradable contract (slow: pacing is ~20 contracts/10min)")
    p.add_argument("--what", nargs="+", default=["BID_ASK"],
                   choices=["BID_ASK", "MIDPOINT", "TRADES"],
                   help="tick types to request (BID_ASK counts double against the budget)")
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help=f"first UTC date to consider (default: {MAX_GAP_DAYS - 1} days "
                        "before --end, the longest window one request can fill)")
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="last UTC date to consider (default: yesterday)")
    p.add_argument("--refetch", nargs="+", type=date.fromisoformat, default=[],
                   metavar="DATE",
                   help="dates inside the window to fetch again although they already "
                        "hold files; bars already stored are not written twice")
    p.add_argument("--max-requests", type=int, default=None,
                   help="cap on requests; whole contracts are dropped and reported")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit without touching the Gateway")
    p.add_argument("--port", type=int, default=None, help="override IBKR_PORT")
    p.add_argument("--mode", choices=["paper", "live"], default=None,
                   help="override IBKR_TRADING_MODE (must match the Gateway login)")
    return p.parse_args(argv)


def load_settings(args: argparse.Namespace) -> IBKRSettings:
    overrides: dict[str, object] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.mode is not None:
        overrides["trading_mode"] = args.mode
    return IBKRSettings(**overrides)


def select_con_ids(resolver: ContractResolver, symbols: list[str],
                   all_symbols: bool) -> list[int]:
    """Tradable conIds for the requested symbols, seeding the cache if empty."""
    con_ids = resolver.tradable_con_ids()
    if not con_ids and SNAPSHOT.exists():
        seeded = resolver.seed_from_snapshot(SNAPSHOT)
        log.info("seeded %d contracts from %s", seeded, SNAPSHOT)
        con_ids = resolver.tradable_con_ids()
    if all_symbols:
        return con_ids
    wanted = {s.upper() for s in symbols}
    return [cid for cid in con_ids
            if ((resolver.get(cid) or {}).get("symbol") or "").upper() in wanted]


def resolve_window(args: argparse.Namespace, data_root: Path) -> tuple[date, date, list[date]]:
    """(gap_start, gap_end, missing dates) inside the requested window.

    Raises ValueError for a window that is reversed or reaches into today (a
    bar of the current hour would be stored incomplete and, once stored, is
    never replaced), and for a ``--refetch`` date outside the window or
    before the available history: dropping it silently would let the
    operator believe it was fetched.
    """
    today = datetime.now(timezone.utc).date()
    end = args.end or today - timedelta(days=1)
    start = args.start or end - timedelta(days=MAX_GAP_DAYS - 1)
    if end < start:
        raise ValueError(f"--end {end} is before --start {start}")
    if end >= today:
        raise ValueError(f"--end {end} is not before today ({today} UTC); only whole "
                         "past days are fetched")
    outside = sorted(d for d in args.refetch if not start <= d <= end)
    if outside:
        raise ValueError(
            f"--refetch {' '.join(d.isoformat() for d in outside)} lies outside the "
            f"window {start}..{end}; widen --start/--end to include it"
        )
    too_early = sorted(d for d in args.refetch if d < HISTORY_STARTS)
    if too_early:
        raise ValueError(
            f"--refetch {' '.join(d.isoformat() for d in too_early)} is before the "
            f"first day with hourly history ({HISTORY_STARTS})"
        )
    gaps = missing_dates(data_root, start, end, refetch=args.refetch)
    if not gaps:
        return start, end, []
    return gaps[0], gaps[-1], gaps


def _check_mode(ib, settings: IBKRSettings) -> None:
    accounts = list(ib.managedAccounts())
    account = accounts[0] if accounts else ""
    mode = "paper" if account.startswith(("DU", "DF")) else "live"
    if not account or mode != settings.trading_mode:
        ib.disconnect()
        raise RuntimeError(
            f"connected account {account!r} looks like {mode} but IBKR_TRADING_MODE="
            f"{settings.trading_mode!r}; refusing to file bars under the wrong session"
        )
    log.warning("ibkr session: account=%s mode=%s role=research read_only=True", account, mode)


def write_summary(payload: dict, data_root: Path) -> Path:
    """Append the run record NEXT TO THE DATA, not into the repo.

    It is provenance for the bars (what was requested, what came back, what
    had no history), so it belongs with them rather than in logs/, which is
    untracked scratch.
    """
    path = data_root / "backfill_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    settings = load_settings(args)
    data_root = settings.resolved_data_root

    try:
        gap_start, gap_end, gaps = resolve_window(args, data_root)
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if not gaps:
        print(f"No missing sweep dates between {gap_start} and {gap_end} "
              f"(history available from {HISTORY_STARTS}); nothing to do.")
        return 0
    span = (gap_end - gap_start).days + 1
    if span > MAX_GAP_DAYS:
        cause = " (--refetch widened the span)" if args.refetch else ""
        print(f"Gap spans {span} days ({gap_start}..{gap_end}), over the "
              f"{MAX_GAP_DAYS}-day single-request ceiling{cause}. Re-run with "
              f"--start/--end in chunks of at most {MAX_GAP_DAYS} days.")
        return 4

    resolver = ContractResolver(settings)
    try:
        con_ids = select_con_ids(resolver, args.symbols, args.all_symbols)
        if not con_ids:
            print("No tradable contracts matched; nothing to request.")
            return 4
        requests, skipped = plan_requests(con_ids, gap_start, gap_end,
                                          what_to_show=args.what,
                                          max_requests=args.max_requests)
        weight = sum(2 if r.what_to_show == "BID_ASK" else 1 for r in requests)
        eta_minutes = weight / 60 * 10  # 60 weighted requests per rolling 10 min
        scope = "ALL symbols" if args.all_symbols else ", ".join(args.symbols)
        print(f"Missing dates ({len(gaps)}): {gaps[0]} .. {gaps[-1]}")
        print(f"Scope: {scope} -> {len(con_ids)} contracts x {len(args.what)} tick "
              f"type(s) = {len(requests)} requests, ~{eta_minutes:.0f} min under pacing")
        sample = requests[0]
        print(f"Request per contract: {sample.duration_str} ending "
              f"{sample.end_dt:%Y-%m-%d %H:%M} UTC (bars before {gap_start} are dropped)")
        not_missing = [gap_start + timedelta(days=i) for i in range(span)
                       if gap_start + timedelta(days=i) not in set(gaps)]
        if not_missing:
            print("Days inside the span that are not missing and will get hourly bars too: "
                  + ", ".join(d.isoformat() for d in not_missing))
        if skipped:
            print(f"WARNING: --max-requests dropped {skipped} contracts entirely "
                  f"(they will still be missing after this run)")
        # Read what the window already holds before anything connects: an
        # unreadable file is found by the plan, not by a run with the venue
        # on the line.
        try:
            stored = read_stored_window(data_root, requests)
        except RuntimeError as exc:
            print(f"refusing: {exc}")
            return 5
        stored_bars = sum(len(keys) for keys in stored.values())
        stored_cons = {key[0] for keys in stored.values() for key in keys}
        print(f"Already stored in the window: {stored_bars} bars, {len(stored_cons)} "
              "contracts (not written again)")
        if args.dry_run:
            print("--dry-run: not connecting.")
            return 0

        lock_target = data_root / "quotes_hourly" / "backfill"
        lock_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with acquire_db_lock(lock_target, stale_after_min=LOCK_STALE_MIN):
                return _run_connected(args, settings, resolver, requests, skipped,
                                      gap_start, gap_end, gaps, data_root)
        except LockHeldError as exc:
            print(f"refusing: another backfill run holds the lock ({exc})")
            return 6
    finally:
        resolver.close()


def _run_connected(args: argparse.Namespace, settings: IBKRSettings, resolver,
                   requests: list, skipped: int, gap_start: date, gap_end: date,
                   gaps: list[date], data_root: Path) -> int:
    """Connect, run the plan, print the outcome and append the run record."""
    session = GatewaySession(settings, "research",
                             connect_validator=lambda ib: _check_mode(ib, settings))
    try:
        ib = session.ensure_connected()
    except KeyboardInterrupt:
        print("Interrupted before connecting.")
        return 3
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backfiller = HistoricalBackfiller(ib, resolver, data_root)
    interrupted = False
    try:
        result = backfiller.run(requests, run_id, skipped_con_ids=skipped)
    except RuntimeError as exc:  # a stored file turned unreadable since the plan
        print(f"refusing: {exc}")
        return 5
    except KeyboardInterrupt:
        # The backfiller flushed what it had; record the run so the operator
        # can finish it with --refetch over the same window.
        interrupted = True
        result = backfiller.last_result
    finally:
        session.close()

    print(f"\nRequests issued : {result.requested}" + (" (INTERRUPTED)" if interrupted else ""))
    print(f"Bars written    : {result.bars} "
          f"({result.contracts_with_bars} contracts had history)")
    print(f"Already stored  : {result.skipped_existing} bars (not written again)")
    print(f"No history      : {len(result.empty_con_ids)} contracts")
    print(f"Failed          : {len(result.failed)}")
    for day, count in sorted(result.dates_written.items()):
        print(f"  {day}: {count} bars")
    for con_id, what, err in result.failed[:10]:
        print(f"  FAIL conId={con_id} {what}: {err}")
    summary = {
        "run_id": run_id,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "mode": settings.trading_mode,
        "gap_start": gap_start.isoformat(),
        "gap_end": gap_end.isoformat(),
        "missing_dates": [d.isoformat() for d in gaps],
        "refetch": [d.isoformat() for d in sorted(set(args.refetch))],
        "symbols": "ALL" if args.all_symbols else args.symbols,
        "what_to_show": args.what,
        "requested": result.requested,
        "interrupted": interrupted,
        "bars": result.bars,
        "skipped_existing": result.skipped_existing,
        "contracts_with_bars": result.contracts_with_bars,
        "contracts_without_history": len(result.empty_con_ids),
        "empty_con_ids": result.empty_con_ids,
        "failed": len(result.failed),
        "failed_requests": [list(item) for item in result.failed],
        "skipped_contracts": result.skipped_con_ids,
        "dates_written": result.dates_written,
        "files": [str(p) for p in result.files],
    }
    print(f"Summary appended to {write_summary(summary, data_root)}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConnectionRefusedError:
        print("Gateway refused the connection: is IB Gateway running and logged in?")
        raise SystemExit(3) from None
