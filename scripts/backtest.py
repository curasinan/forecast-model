"""Backtesting — would our edge signals have made money?

Simulates historical P&L by replaying stored prediction snapshots
against known resolutions.

For each resolved question:
1. Look up our prediction + market price at the time
2. If edge > threshold, simulate a $1 bet in the edge direction
3. Track cumulative P&L, win rate, max drawdown

Usage:
    python scripts/backtest.py
    python scripts/backtest.py --threshold 0.10
    python scripts/backtest.py --predictor ensemble
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import settings
from src.evaluation.metrics import (
    MetricsSummary,
    accuracy,
    brier_score,
    calibration_curve,
    cumulative_ev,
    expected_calibration_error,
    log_loss,
)
from src.evaluation.tracker import EvaluationTracker

console = Console()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def run_backtest(
    tracker: EvaluationTracker,
    predictor_name: str = "ensemble",
    edge_threshold: float = 0.05,
    bet_size: float = 1.0,
) -> None:
    """Run a backtest on stored predictions."""

    resolved = tracker.get_resolved_predictions()
    if not resolved:
        console.print("[yellow]No resolved predictions found. Need market resolutions to backtest.[/yellow]")
        console.print("\nTo add resolutions, use:")
        console.print("  tracker.store_resolution(question_id, outcome=1)  # YES resolved")
        console.print("  tracker.store_resolution(question_id, outcome=0)  # NO resolved")
        _show_demo_backtest()
        return

    # Filter by predictor
    pairs = [
        (s, o) for s, o in resolved
        if s.predictor_name == predictor_name
    ]

    if not pairs:
        available = list({s.predictor_name for s, _ in resolved})
        console.print(f"[yellow]No predictions found for '{predictor_name}'.[/yellow]")
        console.print(f"Available predictors: {', '.join(available)}")
        return

    # Extract data
    preds = [s.probability for s, _ in pairs]
    outcomes = [o for _, o in pairs]
    markets = [s.market_price for s, _ in pairs]
    edges = [s.edge_pct for s, _ in pairs]

    # Simulate trades
    trades: list[dict] = []
    running_pnl = 0.0
    peak_pnl = 0.0
    max_dd = 0.0

    for s, o in pairs:
        edge = s.probability - s.market_price

        if abs(edge) < edge_threshold:
            continue

        if edge > 0:
            # Buy YES
            direction = "YES"
            if o == 1:
                pnl = (1.0 - s.market_price) * bet_size
                result = "WIN"
            else:
                pnl = -s.market_price * bet_size
                result = "LOSS"
        else:
            # Buy NO
            direction = "NO"
            if o == 0:
                pnl = s.market_price * bet_size
                result = "WIN"
            else:
                pnl = -(1.0 - s.market_price) * bet_size
                result = "LOSS"

        running_pnl += pnl
        peak_pnl = max(peak_pnl, running_pnl)
        drawdown = peak_pnl - running_pnl
        max_dd = max(max_dd, drawdown)

        trades.append({
            "question_id": s.question_id[:25],
            "direction": direction,
            "pred": s.probability,
            "market": s.market_price,
            "edge": edge,
            "outcome": o,
            "result": result,
            "pnl": pnl,
            "cumulative": running_pnl,
        })

    # Display results
    console.print(Panel(
        f"[bold]Backtest: {predictor_name}[/bold]\n"
        f"Edge threshold: {edge_threshold:.0%} | Bet size: ${bet_size:.2f}",
        style="blue",
    ))

    if not trades:
        console.print("[yellow]No trades met the edge threshold.[/yellow]")
        return

    # Trade log
    trade_table = Table(title="Trade Log", show_lines=True)
    trade_table.add_column("Question", max_width=25)
    trade_table.add_column("Dir", justify="center")
    trade_table.add_column("Pred", justify="right")
    trade_table.add_column("Market", justify="right")
    trade_table.add_column("Edge", justify="right")
    trade_table.add_column("Result", justify="center")
    trade_table.add_column("P&L", justify="right")
    trade_table.add_column("Cumul.", justify="right")

    for t in trades:
        result_color = "green" if t["result"] == "WIN" else "red"
        pnl_color = "green" if t["pnl"] > 0 else "red"
        trade_table.add_row(
            t["question_id"],
            t["direction"],
            f"{t['pred']:.1%}",
            f"{t['market']:.1%}",
            f"{t['edge']:+.1%}",
            f"[{result_color}]{t['result']}[/{result_color}]",
            f"[{pnl_color}]${t['pnl']:+.2f}[/{pnl_color}]",
            f"${t['cumulative']:+.2f}",
        )

    console.print(trade_table)

    # Summary stats
    wins = sum(1 for t in trades if t["result"] == "WIN")
    total = len(trades)

    summary_table = Table(title="Backtest Summary", show_lines=True)
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", justify="right")

    pnl_color = "green" if running_pnl > 0 else "red"
    summary_table.add_row("Total Trades", str(total))
    summary_table.add_row("Wins / Losses", f"{wins} / {total - wins}")
    summary_table.add_row("Win Rate", f"{wins / total:.1%}")
    summary_table.add_row("Total P&L", f"[{pnl_color}]${running_pnl:+.2f}[/{pnl_color}]")
    summary_table.add_row("Avg P&L per Trade", f"${running_pnl / total:+.2f}")
    summary_table.add_row("Max Drawdown", f"[red]${max_dd:.2f}[/red]")
    summary_table.add_row("", "")
    summary_table.add_row("Brier Score", f"{brier_score(preds, outcomes):.4f}")
    summary_table.add_row("Log Loss", f"{log_loss(preds, outcomes):.4f}")
    summary_table.add_row("Accuracy", f"{accuracy(preds, outcomes):.1%}")

    console.print(summary_table)

    # Calibration
    cal_bins = calibration_curve(preds, outcomes, n_bins=5)
    ece = expected_calibration_error(cal_bins)

    cal_table = Table(title=f"Calibration (ECE: {ece:.4f})", show_lines=True)
    cal_table.add_column("Bucket", justify="center")
    cal_table.add_column("Predicted", justify="right")
    cal_table.add_column("Actual", justify="right")
    cal_table.add_column("Count", justify="right")
    cal_table.add_column("Error", justify="right")

    for b in cal_bins:
        err_color = "green" if b.calibration_error < 0.1 else "yellow" if b.calibration_error < 0.2 else "red"
        cal_table.add_row(
            f"{b.lower:.0%}-{b.upper:.0%}",
            f"{b.predicted_mean:.1%}",
            f"{b.actual_rate:.1%}" if b.count > 0 else "-",
            str(b.count),
            f"[{err_color}]{b.calibration_error:.3f}[/{err_color}]",
        )

    console.print(cal_table)


def _show_demo_backtest():
    """Show a demo backtest with synthetic data to illustrate the format."""
    console.print("\n[bold cyan]--- Demo Backtest (synthetic data) ---[/bold cyan]\n")

    # Synthetic resolved predictions
    demo_trades = [
        {"q": "GPT-5 release", "pred": 0.82, "market": 0.50, "outcome": 1},
        {"q": "Apple AR glasses", "pred": 0.35, "market": 0.60, "outcome": 0},
        {"q": "Claude 5 release", "pred": 0.70, "market": 0.45, "outcome": 1},
        {"q": "AGI claim 2026", "pred": 0.20, "market": 0.30, "outcome": 0},
        {"q": "Gemini Ultra 2", "pred": 0.65, "market": 0.55, "outcome": 1},
    ]

    trade_table = Table(title="Demo Trades", show_lines=True)
    trade_table.add_column("Question", max_width=20)
    trade_table.add_column("Direction")
    trade_table.add_column("Pred", justify="right")
    trade_table.add_column("Market", justify="right")
    trade_table.add_column("Outcome")
    trade_table.add_column("P&L", justify="right")

    total_pnl = 0.0
    for t in demo_trades:
        edge = t["pred"] - t["market"]
        if abs(edge) < 0.05:
            continue

        if edge > 0:
            direction = "YES"
            pnl = (1 - t["market"]) if t["outcome"] == 1 else -t["market"]
        else:
            direction = "NO"
            pnl = t["market"] if t["outcome"] == 0 else -(1 - t["market"])

        total_pnl += pnl
        color = "green" if pnl > 0 else "red"
        trade_table.add_row(
            t["q"],
            direction,
            f"{t['pred']:.0%}",
            f"{t['market']:.0%}",
            "YES" if t["outcome"] == 1 else "NO",
            f"[{color}]${pnl:+.2f}[/{color}]",
        )

    console.print(trade_table)

    color = "green" if total_pnl > 0 else "red"
    console.print(f"\n  Demo Total P&L: [{color}]${total_pnl:+.2f}[/{color}]")
    console.print("\n[dim]This is synthetic data. Add real resolutions to see actual results.[/dim]")


def main():
    parser = argparse.ArgumentParser(description="Backtest prediction edge signals")
    parser.add_argument("--predictor", default="ensemble", help="Predictor to backtest")
    parser.add_argument("--threshold", type=float, default=0.05, help="Min edge to trade")
    parser.add_argument("--bet-size", type=float, default=1.0, help="Bet size in USD")
    args = parser.parse_args()

    tracker = EvaluationTracker(settings.database_path)

    run_backtest(
        tracker=tracker,
        predictor_name=args.predictor,
        edge_threshold=args.threshold,
        bet_size=args.bet_size,
    )


if __name__ == "__main__":
    main()
