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

from climate.paper import parse_ts_utc  # noqa: E402
from climate.paper_criteria import check_staleness  # noqa: E402
from climate.paths import data_root, root_path  # noqa: E402
from src.domain.ids import MarketId  # noqa: E402
from src.models.prediction import EdgeSignal  # noqa: E402
from src.trading.executor import TradeIntent, TradingExecutor, render_group_commitments  # noqa: E402
from src.trading.locks import LockHeldError, acquire_db_lock  # noqa: E402
from src.trading.reconcile import drop_already_committed, load_open_commitments  # noqa: E402
from src.trading.risk_manager import RiskLimits  # noqa: E402
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
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

    # Staleness gate (H2): checked BEFORE the adapter ever connects to the
    # venue -- a stale sweep, stale signal file, or stale SEAS5 init must
    # refuse loudly without touching Gateway at all. An old-format payload
    # (written before this gate existed) is missing sweep_ts_utc/inits
    # entirely; that is treated as its own refusal rather than silently
    # skipped, because check_staleness's "None skips this check" path is
    # reserved for data that is absent BY DESIGN, not data this file simply
    # predates -- an old payload must be regenerated, not trusted. A
    # disk-sourced payload's sweep_ts_utc is likewise NEVER legitimately
    # null (run_gtm_paper.py refuses to write one itself -- see its own
    # staleness gate), so a present-but-null value gets the exact same
    # refusal as an absent key: ``.get()`` returns None either way, so one
    # check covers both.
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
    # Same shared init rule as scripts/run_gtm_paper.py (final-review C-1):
    # "inits" is scoped to the priced ladder by the writer, and each target
    # must carry min(expected latest init, target). A pre-fix payload whose
    # "inits" still lists resolved history refuses here -- regenerate it.
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
    adapter.connect()  # ModeMismatchError unless the account is DU/DF (paper)
    if adapter.mode != "paper":  # belt and braces
        print(f"refusing: connected account {adapter.account} is not paper", file=sys.stderr)
        return 2
    print(f"account={adapter.account} mode={adapter.mode} simulate={args.simulate} "
          f"orders_mode={args.mode}")

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

    # Ruling R3, and the TOCTOU fix from code review: lock FIRST, then
    # snapshot, then place, then sync -- all under the one acquisition.
    # Reading the commitments/resting snapshot before the lock would let
    # two concurrent runs each observe an empty snapshot and then
    # serialize on the lock to both place the same order a moment apart --
    # a real duplicate resting GTC order, exactly what Task 1.7 exists to
    # prevent. A second run must now fail fast at the lock, before it
    # reads anything, not after.
    try:
        with acquire_db_lock(str(db_path)):
            # Placement idempotence + persistent exposure seeding (Task
            # 1.7, audit finding C2): a re-run of this entrypoint against
            # an unchanged signals file must place nothing new for an
            # intent already resting at the venue or already committed in
            # this book, and this fresh process must inherit the exposure
            # a PRIOR process already committed instead of starting back
            # at zero. --simulate keeps the venue read out of a rehearsal
            # (the research adapter is read-only, but reqAllOpenOrders is
            # still a live Gateway call) while still deduping and seeding
            # from its own sim DB.
            commitments = load_open_commitments(str(db_path))
            resting = [] if args.simulate else adapter.get_all_open_orders()
            n_before_dedup = len(intents)
            intents = drop_already_committed(intents, resting, commitments)
            executor.risk_manager.seed_exposure(commitments["total_usd"])
            # H5 correlated-group cap (Task 3.6): the group analogue of the
            # total-exposure seed just above -- inherits what the DB's open
            # book already has riding on each correlated print (e.g. the
            # three resting gtm:2026-12 strikes) so RiskManager.check_trade's
            # group cap sees that committed money even in a brand-new
            # process, not just what THIS run itself places.
            executor.risk_manager.seed_group_exposure(commitments["by_group"])
            print(f"{n_before_dedup - len(intents)} intent(s) dropped as already committed; "
                  f"seeded exposure=${commitments['total_usd']:.2f}")

            report = executor.execute_intents(intents, mode=args.mode)
            print(report.summary())
            # Correlated-group committed-book block (Task 3.6, controller
            # ruling R16 fix for finding I-1): NOT computed from
            # report.decisions alone (that can never show a DB-seeded
            # commitment dedup correctly dropped before the executor ever
            # saw it, e.g. the three resting gtm:2026-12 orders). The
            # dollar figures come from the risk manager's live ledger
            # (seed + this run); the order counts are the DB-seeded count
            # (commitments["by_group_counts"], read above, before this
            # run placed anything) plus this run's own count, added here
            # by the one layer that holds both halves.
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

    runs = data_root() / "paper" / "ibkr_paper_runs.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            # Audit finding M2a: echo the signals payload's model
            # fingerprint (climate.paper.run_paper) so every run-ledger
            # record traces back to the exact fitted model that priced
            # it. .get() rather than [] -- an older/foreign payload
            # written before this field existed must not crash the run;
            # it just gets an honest None here instead.
            "model_fingerprint": payload.get("model_fingerprint"),
            "account": adapter.account, "simulated": executor.is_simulated,
            "mode": args.mode, "intents": len(intents),
            "executed": report.trades_executed, "rejected": report.trades_rejected,
            "orders": [
                {"order_id": d.order.order_id, "market_id": d.order.market_id,
                 "outcome": d.order.outcome, "price": d.order.price, "size": d.order.size,
                 "status": d.order.status.value, "venue_order_id": d.order.venue_order_id,
                 "perm_id": d.order.perm_id,
                 # Audit finding M2b fix round 1 (controller ruling R13):
                 # target-month evidence, additive. question_id is shaped
                 # "gtm:<YYYY-MM>:<strike>" (see load_intents' EdgeSignal
                 # construction above) and is carried straight from the
                 # TradeDecision -- not reconstructed from the order, which
                 # has no target-month field at all.
                 "question_id": d.question_id,
                 "net_ev": d.net_expected_value, "holding_days": d.holding_days}
                for d in report.decisions if d.order is not None
            ],
            # Audit finding M2b fix round 1 (controller ruling R13): the
            # groups (per _group_key -- "gtm:<YYYY-MM>") this run's book
            # already had resting BEFORE this run placed anything, e.g.
            # ["gtm:2026-12"]. Without this, a month whose order was
            # placed in an EARLIER run and is still resting (so dedup in
            # drop_already_committed correctly never lets it re-appear in
            # this run's own orders[]) would never surface in ANY run
            # record's orders[] and so could never count as traded.
            "open_groups": sorted(commitments.get("by_group", {}).keys()),
        }, default=str) + "\n")
    adapter.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
