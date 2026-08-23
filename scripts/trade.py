"""Trade CLI — analyze questions and execute trades.

Usage:
    # Dry-run mode (default): simulate trades, no real money
    python scripts/trade.py --dry-run

    # Approve mode: show each trade, ask for confirmation
    python scripts/trade.py --approve

    # Auto mode: execute all risk-approved trades (use with caution!)
    python scripts/trade.py --auto

    # Analyze specific question(s)
    python scripts/trade.py --dry-run --question gpt5-release-2025

    # Show portfolio state
    python scripts/trade.py --portfolio

    # Adjust bankroll and risk limits
    python scripts/trade.py --dry-run --bankroll 500 --max-position 25
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.researcher import Researcher
from src.agent.scheduler import load_questions
from src.config import settings
from src.evaluation.tracker import EvaluationTracker
from src.storage.database import Database
from src.trading.executor import TradingExecutor
from src.trading.risk_manager import RiskLimits

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze predictions and execute trades on Polymarket"
    )

    # Mode (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate trades locally (default)",
    )
    mode_group.add_argument(
        "--approve",
        action="store_true",
        help="Show each trade for human approval before execution",
    )
    mode_group.add_argument(
        "--auto",
        action="store_true",
        help="Auto-execute all risk-approved trades (caution!)",
    )

    # Filtering
    parser.add_argument(
        "--question", "-q",
        type=str,
        help="Analyze a specific question ID only",
    )
    parser.add_argument(
        "--edge-only",
        action="store_true",
        help="Only show/trade questions with detected edge",
    )

    # Portfolio view
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Show current portfolio state and exit",
    )

    # Risk parameters
    parser.add_argument("--bankroll", type=float, default=1000.0, help="Starting bankroll ($)")
    parser.add_argument("--max-position", type=float, default=50.0, help="Max $ per trade")
    parser.add_argument("--max-exposure", type=float, default=200.0, help="Max total exposure ($)")
    parser.add_argument("--max-daily-loss", type=float, default=50.0, help="Daily loss limit ($)")
    parser.add_argument("--min-edge", type=float, default=0.05, help="Minimum edge %% to trade")

    # Live trading (disabled by default)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (requires private key in .env)",
    )
    parser.add_argument("--private-key", type=str, default="", help="Polygon wallet private key")

    return parser.parse_args()


async def run_analysis(questions, args):
    """Run the research pipeline on questions."""
    db = Database(settings.database_path)
    tracker = EvaluationTracker(settings.database_path)
    researcher = Researcher(db, tracker)

    print(f"\nAnalyzing {len(questions)} question(s)...\n")
    results = await researcher.analyze_all(questions)

    db.close()

    if args.edge_only:
        results = [r for r in results if r.has_edge]
        print(f"Filtered to {len(results)} question(s) with detected edge.\n")

    return results


def show_analysis_summary(results):
    """Print a summary of analysis results."""
    print("\n" + "=" * 70)
    print("ANALYSIS SUMMARY")
    print("=" * 70)

    for r in results:
        edge_marker = ">>>" if r.has_edge else "   "
        print(
            f"  {edge_marker} {r.question.id[:30]:<30} "
            f"ensemble={r.ensemble.probability:.0%}  "
            f"market={r.edge.market_prob:.0%}  "
            f"edge={r.edge.edge_pct:+.1%}  "
            f"EV=${r.edge.expected_value:+.3f}  "
            f"VPIN={r.vpin_level}"
        )

    edge_count = sum(1 for r in results if r.has_edge)
    print(f"\n  {edge_count}/{len(results)} questions have tradeable edge.\n")


def show_portfolio(executor):
    """Display current portfolio state."""
    portfolio = executor.get_portfolio()

    print("\n" + "=" * 50)
    print("PORTFOLIO STATE")
    print("=" * 50)

    w = portfolio["wallet"]
    print(f"  Wallet: {'DRY-RUN' if w['dry_run'] else 'LIVE'}")
    print(f"  Connected: {w['connected']}")
    print(f"  USDCe: ${w['balance']['usdce']:.2f}")

    r = portfolio["risk"]
    print(f"\n  Bankroll: ${r['bankroll']:.2f}")

    p = portfolio["positions"]
    print(f"\n  Open positions: {p['open_positions']}")
    print(f"  Total exposure: ${p['total_exposure_usd']:.2f}")
    print(f"  Realized P&L: ${p['realized_pnl']:+.2f}")
    print(f"  Total trades: {p['total_trades']}")

    o = portfolio["orders"]
    print(f"\n  Session orders: {o['total_orders']}")
    print(f"  Filled: {o['filled']}")
    print(f"  Rejected: {o['rejected']}")
    print(f"  Session cost: ${o['total_cost_usd']:.2f}")
    print("=" * 50)


async def main():
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("scripts/trade.py")
    args = parse_args()

    # Determine mode
    if args.auto:
        mode = "auto"
        dry_run = args.live is False  # Auto can be dry-run too
    elif args.approve:
        mode = "approve"
        dry_run = args.live is False
    else:
        mode = "dry_run"
        dry_run = True

    if args.live:
        dry_run = False
        private_key = args.private_key
        if not private_key:
            print("ERROR: --live requires --private-key or POLYMARKET_PRIVATE_KEY in .env")
            sys.exit(1)
        print("\n*** LIVE TRADING MODE — Real money will be used! ***\n")
    else:
        private_key = ""

    # Build risk limits
    risk_limits = RiskLimits(
        min_edge_pct=args.min_edge,
        max_position_usd=args.max_position,
        max_total_exposure_usd=args.max_exposure,
        max_daily_loss_usd=args.max_daily_loss,
    )

    # Create executor
    executor = TradingExecutor(
        dry_run=dry_run,
        private_key=private_key,
        bankroll=args.bankroll,
        risk_limits=risk_limits,
    )

    # Portfolio-only mode
    if args.portfolio:
        executor.connect()
        show_portfolio(executor)
        return

    # Connect
    if not executor.connect():
        print("ERROR: Failed to connect wallet.")
        sys.exit(1)

    # Load questions
    questions = load_questions()
    if not questions:
        print("No questions found in data/questions/sample_questions.yaml")
        sys.exit(1)

    # Filter to specific question if requested
    if args.question:
        questions = [q for q in questions if q.id == args.question]
        if not questions:
            print(f"Question '{args.question}' not found.")
            sys.exit(1)

    # Filter expired
    from datetime import datetime
    questions = [q for q in questions if q.deadline > datetime.now()]
    print(f"Loaded {len(questions)} open question(s).")

    # Run analysis
    results = await run_analysis(questions, args)

    if not results:
        print("No results to trade.")
        return

    # Show summary
    show_analysis_summary(results)

    # Execute trades
    if mode == "dry_run":
        print("[DRY-RUN MODE] Simulating all risk-approved trades...\n")
    elif mode == "approve":
        print("[APPROVE MODE] You will be asked to confirm each trade.\n")
    elif mode == "auto":
        label = "LIVE" if not dry_run else "DRY-RUN"
        print(f"[AUTO MODE / {label}] Executing all risk-approved trades...\n")

    report = executor.execute(results, mode=mode)

    # Show report
    print("\n" + report.summary())

    # Show portfolio after trading
    show_portfolio(executor)


if __name__ == "__main__":
    asyncio.run(main())
