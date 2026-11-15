"""IBKR PAPER trading entrypoint for the climate lane (GTM strike ladder)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from climate.paper import parse_ts_utc  # noqa: E402
from climate.paper_criteria import check_staleness  # noqa: E402
from climate.paths import data_root, root_path  # noqa: E402
from src.trading.executor import TradeIntent, TradingExecutor, render_group_commitments  # noqa: E402
from src.trading.gtm_intents import intents_from_payload  # noqa: E402
from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.trading.pricing import DEFAULT_PRICE_POLICY, PRICE_POLICIES  # noqa: E402
from src.trading.reconcile import drop_already_committed, load_open_commitments  # noqa: E402
from src.trading.reprice import RepriceOutcome, reprice_resting_orders  # noqa: E402
from src.trading.risk_manager import RiskLimits  # noqa: E402
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
from src.venues.ibkr.adapter import IBKRAdapter  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("run_ibkr_paper")


def load_intents(signals_path: Path, adapter: IBKRAdapter, top: int | None,
                 min_edge: float, *, price_policy: str = DEFAULT_PRICE_POLICY,
                 ticks_below_ask: int = 0) -> list[TradeIntent]:
    """gtm_signals.json at `signals_path` as TradeIntents (src/trading/gtm_intents.py)."""
    payload = json.loads(signals_path.read_text(encoding="utf-8"))
    return intents_from_payload(payload, adapter, top, min_edge, price_policy=price_policy,
                                ticks_below_ask=ticks_below_ask)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IBKR paper trading on GTM signals (paper only)")
    ap.add_argument("--signals", default=None, help="path to gtm_signals.json")
    ap.add_argument("--mode", choices=["approve", "auto"], default="approve")
    ap.add_argument("--simulate", action="store_true",
                    help="local simulated fills only; read-only adapter, no venue orders")
    ap.add_argument("--top", type=int, default=None, help="only the N largest-edge signals")
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="minimum edge at the limit price actually paid (default 0.05)")
    ap.add_argument("--price", choices=PRICE_POLICIES, default=DEFAULT_PRICE_POLICY,
                    help="limit rule: 'ask' bids the ask (fills against the resting offer), "
                         "'mid' bids the mid (rests until a seller comes down). Default ask")
    ap.add_argument("--ticks-below-ask", type=int, default=0,
                    help="with --price ask: bid this many ticks under the ask, never under "
                         "the bid (default 0, a limit at the ask)")
    ap.add_argument("--reprice", action="store_true",
                    help="cancel resting book orders priced below today's limit and buy "
                         "again at the new limit; needs the venue (not with --simulate)")
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--max-position", type=float, default=10.0)
    ap.add_argument("--max-exposure", type=float, default=50.0)
    ap.add_argument("--max-group", type=float, default=RiskLimits().max_group_usd,
                    help="cap on the dollars committed to one target month, the strikes of "
                         "one print being one correlated bet (default %(default)s)")
    ap.add_argument("--db", default=None,
                    help="paper position DB (default <IBKR data root>/paper/ibkr_paper.db)")
    args = ap.parse_args(argv)
    if args.ticks_below_ask < 0:
        print("refusing: --ticks-below-ask cannot be negative", file=sys.stderr)
        return 2
    if args.reprice and args.simulate:
        print("refusing: --reprice cancels resting venue orders; it cannot run with --simulate",
              file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = IBKRSettings()
    if settings.trading_mode != "paper":
        print("refusing: this is the PAPER entrypoint; IBKR_TRADING_MODE must be 'paper'",
              file=sys.stderr)
        return 2

    try:
        refuse_if_mirror(settings.resolved_data_root, "place or simulate paper orders")
        if args.db:
            refuse_if_mirror(Path(args.db).parent, "place or simulate paper orders")
        refuse_if_mirror(root_path(), "place or simulate paper orders")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    signals_path = Path(args.signals) if args.signals else data_root() / "paper" / "gtm_signals.json"
    if not signals_path.exists():
        print(f"no signals at {signals_path}; run scripts/run_gtm_paper.py first", file=sys.stderr)
        return 1

    payload = json.loads(signals_path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if payload.get("sweep_ts_utc") is None:
        reasons.append(
            "signals payload has no usable 'sweep_ts_utc' (missing key, or a null "
            "value from an unparseable sweep filename -- old/invalid file; "
            "regenerate with scripts/run_gtm_paper.py)"
        )
    if "inits" not in payload:
        reasons.append(
            "signals payload missing 'inits' (old-format file -- regenerate with "
            "scripts/run_gtm_paper.py)"
        )
    sweep_ts = parse_ts_utc(payload["sweep_ts_utc"]) if payload.get("sweep_ts_utc") else None
    signal_ts = parse_ts_utc(payload["ts_utc"]) if payload.get("ts_utc") else None
    reasons += check_staleness(
        sweep_ts=sweep_ts,
        signal_ts=signal_ts,
        inits_by_target=payload.get("inits"),
        now=datetime.now(timezone.utc),
    )
    if reasons:
        for reason in reasons:
            print(reason, file=sys.stderr)
        return 1

    if args.simulate:
        adapter = IBKRAdapter(settings, role="research", read_only=True)
    else:
        adapter = IBKRAdapter(settings, role="live_trading", read_only=False)  # clientId 1
    adapter.connect()
    if adapter.mode != "paper":  # belt and braces
        print(f"refusing: connected account {adapter.account} is not paper", file=sys.stderr)
        return 2
    print(f"account={adapter.account} mode={adapter.mode} simulate={args.simulate} "
          f"orders_mode={args.mode}")

    intents = load_intents(signals_path, adapter, args.top, args.min_edge,
                           price_policy=args.price, ticks_below_ask=args.ticks_below_ask)
    print(f"{len(intents)} intents from {signals_path} (price={args.price}, "
          f"ticks_below_ask={args.ticks_below_ask})")

    default_db = "ibkr_paper_sim.db" if args.simulate else "ibkr_paper.db"
    db_path = Path(args.db) if args.db else settings.resolved_data_root / "paper" / default_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"paper book: {db_path}")

    executor = TradingExecutor(
        venue=adapter, bankroll=args.bankroll,
        risk_limits=RiskLimits(min_edge_pct=args.min_edge, max_position_usd=args.max_position,
                               max_total_exposure_usd=args.max_exposure,
                               max_group_usd=args.max_group),
        simulate=args.simulate, db_path=str(db_path),
    )

    repriced = RepriceOutcome()
    try:
        with acquire_db_lock(str(db_path)):
            commitments = load_open_commitments(str(db_path))
            resting = [] if args.simulate else adapter.get_all_open_orders()
            if args.reprice:
                repriced = reprice_resting_orders(
                    executor, adapter, str(db_path), intents, resting, commitments,
                    mode=args.mode,
                )
                for note in repriced.notes:
                    print(f"reprice: {note}")
                print(f"reprice: {len(repriced.cancelled_perm_ids)} resting order(s) cancelled, "
                      f"{len(repriced.replacements)} to be bought again at the new limit")
                if repriced.cancelled_perm_ids:
                    commitments = load_open_commitments(str(db_path))
                    resting = [o for o in resting
                               if str((o.venue_meta or {}).get("perm_id") or "")
                               not in repriced.cancelled_perm_ids]
            n_before_dedup = len(intents)
            intents = drop_already_committed(intents, resting, commitments)
            executor.risk_manager.seed_exposure(commitments["total_usd"])
            executor.risk_manager.seed_group_exposure(commitments["by_group"])
            print(f"{n_before_dedup - len(intents)} intent(s) dropped as already committed; "
                  f"seeded exposure=${commitments['total_usd']:.2f}")

            report = executor.execute_intents(intents, mode=args.mode,
                                              pre_approved=repriced.replacements)
            print(report.summary())
            group_counts = dict(commitments["by_group_counts"])
            for key, n in report.group_order_counts().items():
                group_counts[key] = group_counts.get(key, 0) + n
            group_block = render_group_commitments(executor.risk_manager.group_exposure(), group_counts)
            if group_block:
                print(group_block)
            if not executor.is_simulated:
                updated = executor.sync()
                print(f"synced with venue: {updated} order(s) updated")
    except LockHeldError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        adapter.disconnect()
        return 1

    portfolio = executor.get_portfolio()
    print(json.dumps(portfolio, indent=2, default=str))

    quotes = {i.edge.question_id: i.quote for i in intents}
    runs = data_root() / "paper" / "ibkr_paper_runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "model_fingerprint": payload.get("model_fingerprint"),
            "account": adapter.account, "simulated": executor.is_simulated,
            "mode": args.mode, "intents": len(intents),
            "price_policy": args.price, "ticks_below_ask": args.ticks_below_ask,
            "repriced": repriced.records,
            "executed": report.trades_executed, "rejected": report.trades_rejected,
            "orders": [
                {"order_id": d.order.order_id, "market_id": d.order.market_id,
                 "outcome": d.order.outcome, "price": d.order.price, "size": d.order.size,
                 "status": d.order.status.value, "venue_order_id": d.order.venue_order_id,
                 "perm_id": d.order.perm_id,
                 "question_id": d.question_id,
                 "net_ev": d.net_expected_value, "holding_days": d.holding_days,
                 "quote": quotes.get(d.question_id)}
                for d in report.decisions if d.order is not None
            ],
            "open_groups": sorted(commitments.get("by_group", {}).keys()),
        }, default=str) + "\n")
    adapter.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
