"""IBKR PAPER trading entrypoint for the climate lane (GTM strike ladder).

Turns the research signals written by scripts/run_gtm_paper.py
(%LOCALAPPDATA%/climate_data/paper/gtm_signals.json) into TradeIntents and
runs them through TradingExecutor on an IBKRAdapter.

Safety properties (all enforced in code, not by convention):
- Refuses to start unless IBKR_TRADING_MODE=paper (settings) AND the
  connected account is a paper account (DU/DF prefix, verified by the
  adapter). A live account never gets past connect().
- The adapter itself refuses to transmit on a live account even if the
  above were bypassed (src/venues/ibkr/adapter.py::_place_order).
- --simulate runs the same pipeline with local simulated fills and a
  read-only adapter (no order can reach the venue at all).
- Default mode is "approve": every order is shown and confirmed.

Usage:
    set IBKR_PORT=4002  IBKR_TRADING_MODE=paper   (Gateway logged into PAPER)
    python scripts/run_ibkr_paper.py                 # approve each order
    python scripts/run_ibkr_paper.py --simulate      # no venue orders
    python scripts/run_ibkr_paper.py --mode auto --top 5
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

from climate.paths import data_root  # noqa: E402
from src.domain.ids import MarketId  # noqa: E402
from src.models.prediction import EdgeSignal  # noqa: E402
from src.trading.executor import TradeIntent, TradingExecutor  # noqa: E402
from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.trading.reconcile import drop_already_committed, load_open_commitments  # noqa: E402
from src.trading.risk_manager import RiskLimits  # noqa: E402
from src.venues.ibkr.adapter import IBKRAdapter  # noqa: E402
from src.venues.ibkr.settings import IBKRSettings  # noqa: E402

log = logging.getLogger("run_ibkr_paper")


def load_intents(signals_path: Path, adapter: IBKRAdapter, top: int | None,
                 min_edge: float) -> list[TradeIntent]:
    """Map gtm_signals.json rows to TradeIntents (YES or NO leg per sign of edge)."""
    payload = json.loads(signals_path.read_text(encoding="utf-8"))
    rows = [r for r in payload.get("signals", []) if r.get("tradeable")]
    rows.sort(key=lambda r: -abs(float(r.get("edge") or 0.0)))
    if top:
        rows = rows[:top]
    intents: list[TradeIntent] = []
    for r in rows:
        p = float(r["p_model"])
        yes_mid = r.get("yes_mid")
        if yes_mid is None or r.get("suspect_quote"):
            continue
        yes_mid = float(yes_mid)
        edge = p - yes_mid
        if abs(edge) < min_edge:
            continue
        market_id = MarketId.ibkr(int(r["con_id"]))
        try:
            market = adapter.get_market(market_id)
        except Exception as exc:  # noqa: BLE001 -- skip, don't guess a contract
            log.warning("skipping conId %s: %s", r["con_id"], exc)
            continue
        title = f"GTM {r['target_month']} > {r['strike']}"
        signal = EdgeSignal(
            question_id=f"gtm:{r['target_month']}:{r['strike']}",
            model_prob=p, market_prob=yes_mid, edge_pct=edge,
            expected_value=float(r.get("ev_per_contract_after_fee") or edge),
            kelly_fraction=float(r.get("half_kelly") or 0.0),
            confidence=1.0,
        )
        intents.append(TradeIntent(
            edge=signal, market=market, title=title,
            no_price=float(r["no_mid"]) if r.get("no_mid") is not None else None,
        ))
    return intents


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IBKR paper trading on GTM signals (paper only)")
    ap.add_argument("--signals", default=None, help="path to gtm_signals.json")
    ap.add_argument("--mode", choices=["approve", "auto"], default="approve")
    ap.add_argument("--simulate", action="store_true",
                    help="local simulated fills only; read-only adapter, no venue orders")
    ap.add_argument("--top", type=int, default=None, help="only the N largest-edge signals")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--max-position", type=float, default=10.0)
    ap.add_argument("--max-exposure", type=float, default=50.0)
    ap.add_argument("--db", default=None,
                    help="paper position DB (default <IBKR data root>/paper/ibkr_paper.db)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = IBKRSettings()  # raises if IBKR_PORT / IBKR_TRADING_MODE are missing
    if settings.trading_mode != "paper":
        print("refusing: this is the PAPER entrypoint; IBKR_TRADING_MODE must be 'paper'",
              file=sys.stderr)
        return 2

    if args.simulate:
        adapter = IBKRAdapter(settings, role="research", read_only=True)
    else:
        adapter = IBKRAdapter(settings, role="live_trading", read_only=False)  # clientId 1
    adapter.connect()  # ModeMismatchError unless the account is DU/DF (paper)
    if adapter.mode != "paper":  # belt and braces
        print(f"refusing: connected account {adapter.account} is not paper", file=sys.stderr)
        return 2
    print(f"account={adapter.account} mode={adapter.mode} simulate={args.simulate} "
          f"orders_mode={args.mode}")

    signals_path = Path(args.signals) if args.signals else data_root() / "paper" / "gtm_signals.json"
    if not signals_path.exists():
        print(f"no signals at {signals_path}; run scripts/run_gtm_paper.py first", file=sys.stderr)
        return 1
    intents = load_intents(signals_path, adapter, args.top, args.min_edge)
    print(f"{len(intents)} intents from {signals_path}")

    # Its OWN database, NOT the repo's data/predictions.db: that file still
    # holds March-2026 Polymarket-era simulated test positions, and the
    # 365-day paper record has to be clean to mean anything. It also lives
    # outside OneDrive (SQLite + cloud sync corrupts) and outside AppData
    # (packaged-app virtualisation -- see MIGRATION_CHECKPOINT section 3).
    # --simulate keeps its own file: a rehearsal's invented fills must never
    # land in the book that the 365-day go-live gate is measured on.
    default_db = "ibkr_paper_sim.db" if args.simulate else "ibkr_paper.db"
    db_path = Path(args.db) if args.db else settings.resolved_data_root / "paper" / default_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"paper book: {db_path}")

    executor = TradingExecutor(
        venue=adapter, bankroll=args.bankroll,
        risk_limits=RiskLimits(min_edge_pct=args.min_edge, max_position_usd=args.max_position,
                               max_total_exposure_usd=args.max_exposure),
        simulate=args.simulate, db_path=str(db_path),
    )

    # Placement idempotence + persistent exposure seeding (Task 1.7, audit
    # finding C2): a re-run of this entrypoint against an unchanged signals
    # file must place nothing new for an intent already resting at the
    # venue or already committed in this book, and this fresh process must
    # inherit the exposure a PRIOR process already committed instead of
    # starting back at zero. --simulate keeps the venue read out of a
    # rehearsal (the research adapter is read-only, but reqAllOpenOrders is
    # still a live Gateway call) while still deduping and seeding from its
    # own sim DB.
    commitments = load_open_commitments(str(db_path))
    resting = [] if args.simulate else adapter.get_all_open_orders()
    n_before_dedup = len(intents)
    intents = drop_already_committed(intents, resting, commitments)
    executor.risk_manager.seed_exposure(commitments["total_usd"])
    print(f"{n_before_dedup - len(intents)} intent(s) dropped as already committed; "
          f"seeded exposure=${commitments['total_usd']:.2f}")

    # Ruling R3: the placement + sync block is the only part of a run that
    # writes to db_path, so it is the only part that needs exclusion from a
    # concurrent writer of the same book (an operator's manual run racing
    # the scheduled sync job, Task 1.6).
    try:
        with acquire_db_lock(str(db_path)):
            report = executor.execute_intents(intents, mode=args.mode)
            print(report.summary())
            if not executor.is_simulated:
                updated = executor.sync()
                print(f"synced with venue: {updated} order(s) updated")
    except LockHeldError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        adapter.disconnect()
        return 1

    portfolio = executor.get_portfolio()
    print(json.dumps(portfolio, indent=2, default=str))

    runs = data_root() / "paper" / "ibkr_paper_runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "account": adapter.account, "simulated": executor.is_simulated,
            "mode": args.mode, "intents": len(intents),
            "executed": report.trades_executed, "rejected": report.trades_rejected,
            "orders": [
                {"order_id": d.order.order_id, "market_id": d.order.market_id,
                 "outcome": d.order.outcome, "price": d.order.price, "size": d.order.size,
                 "status": d.order.status.value, "venue_order_id": d.order.venue_order_id,
                 "perm_id": d.order.perm_id,
                 "net_ev": d.net_expected_value, "holding_days": d.holding_days}
                for d in report.decisions if d.order is not None
            ],
        }, default=str) + "\n")
    adapter.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
