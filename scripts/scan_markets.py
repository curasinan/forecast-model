"""Scan Polymarket for high-entropy tech markets.

Ranks markets by opportunity score = entropy × volume.
High uncertainty + enough liquidity = best targets for our model.

Usage:
    python scripts/scan_markets.py
"""

import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.polymarket import PolymarketCollector
from src.math.entropy import binary_entropy, opportunity_score

console = Console()


async def main():
    console.print("[bold]Scanning Polymarket for tech/AI markets...[/bold]\n")

    collector = PolymarketCollector()
    markets = await collector.get_all_tech_markets(limit=30)

    if not markets:
        console.print("[yellow]No markets found. Polymarket API may be unavailable.[/yellow]")
        return

    # Calculate entropy and opportunity scores
    for m in markets:
        price = m.get("yes_price", 0.5)
        vol = m.get("volume_24h", 0) or m.get("volume", 0)
        m["entropy"] = binary_entropy(price)
        m["opp_score"] = opportunity_score(price, vol)

    # Sort by opportunity score
    markets.sort(key=lambda m: m["opp_score"], reverse=True)

    # Display
    table = Table(title=f"Top Tech Markets by Opportunity ({len(markets)} found)")
    table.add_column("#", style="dim", width=3)
    table.add_column("Market", max_width=55)
    table.add_column("YES $", justify="right", style="green")
    table.add_column("Entropy", justify="right", style="yellow")
    table.add_column("Volume", justify="right", style="cyan")
    table.add_column("Opp Score", justify="right", style="bold magenta")

    for i, m in enumerate(markets[:25], 1):
        price = m.get("yes_price", 0.5)
        volume = m.get("volume_24h", 0) or m.get("volume", 0)
        question = m.get("question", "")[:55]

        # Color-code price by uncertainty
        if 0.35 <= price <= 0.65:
            price_str = f"[bold green]${price:.2f}[/bold green]"
        else:
            price_str = f"${price:.2f}"

        table.add_row(
            str(i),
            question,
            price_str,
            f"{m['entropy']:.3f}",
            f"${volume:,.0f}",
            f"{m['opp_score']:.3f}",
        )

    console.print(table)

    # Summary
    high_entropy = [m for m in markets if m["entropy"] > 0.9]
    console.print(f"\n[bold]Markets with high uncertainty (entropy > 0.9): {len(high_entropy)}[/bold]")
    console.print("[dim]These markets have the most room for your model to add value.[/dim]")

    console.print(
        "\n[dim]Tip: Add interesting markets to data/questions/sample_questions.yaml "
        "and run scripts/run_prediction.py to predict them.[/dim]"
    )


if __name__ == "__main__":
    asyncio.run(main())
