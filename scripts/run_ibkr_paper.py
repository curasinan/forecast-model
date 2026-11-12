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
from src.domain.ids import MarketId  # noqa: E402
from src.models.prediction import EdgeSignal  # noqa: E402
from src.trading.executor import TradeIntent, TradingExecutor, render_group_commitments  # noqa: E402
from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.trading.pricing import (  # noqa: E402
    DEFAULT_PRICE_POLICY,
    DEFAULT_TICK,
    PRICE_POLICIES,
    buy_limit_price,
    finite_or_none,
)
from src.trading.reconcile import drop_already_committed, load_open_commitments  # noqa: E402
from src.trading.reprice import RepriceOutcome, reprice_resting_orders  # noqa: E402
from src.trading.risk_manager import RiskLimits  # noqa: E402
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
from src.venues.ibkr.adapter import IBKRAdapter  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("run_ibkr_paper")


def _leg_limit(row: dict, buy_yes: bool, *, price_policy: str, tick: float,
               ticks_below_ask: int) -> float | None:
    """The limit for the leg being bought, from that leg's own bid and ask."""
    if buy_yes:
        return buy_limit_price(row.get("bid"), row.get("ask"), policy=price_policy, tick=tick,
                               ticks_below_ask=ticks_below_ask)
    return buy_limit_price(row.get("no_bid"), row.get("no_ask"), policy=price_policy, tick=tick,
                           ticks_below_ask=ticks_below_ask)


def load_intents(signals_path: Path, adapter: IBKRAdapter, top: int | None,
                 min_edge: float, *, price_policy: str = DEFAULT_PRICE_POLICY,
                 ticks_below_ask: int = 0) -> list[TradeIntent]:
    """Map gtm_signals.json rows to TradeIntents, priced at the limit the order will carry.

    The leg follows the sign of the edge at the mid (YES when the model is
    above the market). The limit comes from that leg's quote under
    `price_policy` ('ask': the ask less `ticks_below_ask` ticks; 'mid': the
    mid), and the edge and EV are taken at that limit, the price actually
    paid. A row whose edge at the limit is below `min_edge` is skipped. The
    intents come back sorted by that edge, largest first.
    """
    payload = json.loads(signals_path.read_text(encoding="utf-8"))
    rows = [r for r in payload.get("signals", []) if r.get("tradeable")]
    rows.sort(key=lambda r: -abs(float(r.get("edge") or 0.0)))
    if top:
        rows = rows[:top]
    fee_model = adapter.fee_model()  # refuses stale fee facts before anything is priced
    intents: list[TradeIntent] = []
    for r in rows:
        p = float(r["p_model"])
        yes_mid = finite_or_none(r.get("yes_mid"))
        if yes_mid is None or r.get("suspect_quote"):
            continue
        buy_yes = p - yes_mid > 0
        question_id = f"gtm:{r['target_month']}:{r['strike']}"
        limits = dict(price_policy=price_policy, ticks_below_ask=ticks_below_ask)

        price = _leg_limit(r, buy_yes, tick=DEFAULT_TICK, **limits)
        if price is None:
            log.warning("skipping %s: no usable %s quote", question_id, "YES" if buy_yes else "NO")
            continue
        if (p - price if buy_yes else (1.0 - p) - price) < min_edge:
            continue

        market_id = MarketId.ibkr(int(r["con_id"]))
        try:
            market = adapter.get_market(market_id)
        except Exception as exc:  # noqa: BLE001 -- skip, don't guess a contract
            log.warning("skipping conId %s: %s", r["con_id"], exc)
            continue
        tick = market.tick_size or DEFAULT_TICK
        if tick != DEFAULT_TICK:
            price = _leg_limit(r, buy_yes, tick=tick, **limits)
            if price is None:
                continue

        edge = p - price if buy_yes else (1.0 - p) - price
        if edge < min_edge:
            continue
        yes_price = price if buy_yes else fee_model.yes_no_price_sum() - price
        title = f"GTM {r['target_month']} > {r['strike']}"
        signal = EdgeSignal(
            question_id=question_id,
            model_prob=p, market_prob=yes_price, edge_pct=edge if buy_yes else -edge,
            expected_value=edge - fee_model.execution_cost(price, 1),
            kelly_fraction=float(r.get("half_kelly") or 0.0),
            confidence=1.0,
        )
        leg = "" if buy_yes else "no_"
        intents.append(TradeIntent(
            edge=signal, market=market, title=title,
            no_price=None if buy_yes else price,
            quote={"policy": price_policy, "ticks_below_ask": ticks_below_ask,
                   "bid": finite_or_none(r.get(f"{leg}bid")),
                   "ask": finite_or_none(r.get(f"{leg}ask")),
                   "limit": price},
        ))
    intents.sort(key=lambda i: -abs(i.edge.edge_pct))  # the group cap goes to the best edge first
    return intents


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
