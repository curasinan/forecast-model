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

Exit codes: 0 done (bars written or nothing missing), 3 Gateway offline /
connection refused, 4 nothing to request (no contracts matched).
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

from src.venues.ibkr.backfill import (  # noqa: E402
    HISTORY_STARTS,
    MAX_DURATION_DAYS,
    HistoricalBackfiller,
    missing_dates,
    plan_requests,
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
                   help="first UTC date to consider (default: 30 days ago)")
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="last UTC date to consider (default: yesterday)")
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
    """(gap_start, gap_end, missing dates) inside the requested window."""
    today = datetime.now(timezone.utc).date()
    end = args.end or today - timedelta(days=1)
    start = args.start or end - timedelta(days=29)
    gaps = missing_dates(data_root, start, end)
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


def write_summary(payload: dict) -> Path:
    path = REPO_ROOT / "logs" / "ibkr_backfill_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    settings = load_settings(args)
    data_root = settings.resolved_data_root

    gap_start, gap_end, gaps = resolve_window(args, data_root)
    if not gaps:
        print(f"No missing sweep dates between {gap_start} and {gap_end} "
              f"(history available from {HISTORY_STARTS}); nothing to do.")
        return 0
    span = (gap_end - gap_start).days + 1
    if span > MAX_DURATION_DAYS:
        print(f"Gap spans {span} days ({gap_start}..{gap_end}), over the "
              f"{MAX_DURATION_DAYS}-day single-request ceiling. Re-run with "
              f"--start/--end in chunks of at most {MAX_DURATION_DAYS} days.")
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
        if skipped:
            print(f"WARNING: --max-requests dropped {skipped} contracts entirely "
                  f"(they will still be missing after this run)")
        if args.dry_run:
            print("--dry-run: not connecting.")
            return 0

        session = GatewaySession(settings, "research",
                                 connect_validator=lambda ib: _check_mode(ib, settings))
        try:
            ib = session.ensure_connected()
        except KeyboardInterrupt:
            print("Interrupted before connecting.")
            return 3
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backfiller = HistoricalBackfiller(ib, resolver, data_root)
        try:
            result = backfiller.run(requests, run_id, skipped_con_ids=skipped)
        finally:
            session.close()
    finally:
        resolver.close()

    print(f"\nRequests issued : {result.requested}")
    print(f"Bars written    : {result.bars} "
          f"({result.contracts_with_bars} contracts had history)")
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
        "symbols": "ALL" if args.all_symbols else args.symbols,
        "what_to_show": args.what,
        "requested": result.requested,
        "bars": result.bars,
        "contracts_with_bars": result.contracts_with_bars,
        "contracts_without_history": len(result.empty_con_ids),
        "failed": len(result.failed),
        "skipped_contracts": result.skipped_con_ids,
        "dates_written": result.dates_written,
        "files": [str(p) for p in result.files],
    }
    print(f"Summary appended to {write_summary(summary)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConnectionRefusedError:
        print("Gateway refused the connection: is IB Gateway running and logged in?")
        raise SystemExit(3) from None
