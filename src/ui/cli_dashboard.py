"""Rich CLI Dashboard — live view of the prediction system.

Displays:
- Edge detection table: question | our prob | market | edge | EV | Kelly | VPIN
- Predictor breakdown per question
- Monte Carlo confidence intervals
- Calibration stats and historical accuracy
- Market scanner (entropy-ranked opportunities)

Uses Rich's Live display for auto-refreshing output.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.agent.researcher import ResearchResult, Researcher
from src.agent.scheduler import Scheduler, load_questions
from src.config import settings
from src.edge.market_selector import scan_markets, filter_actionable
from src.evaluation.metrics import compare_predictors
from src.evaluation.tracker import EvaluationTracker
from src.storage.database import Database

logger = logging.getLogger(__name__)


def _safe(text: str) -> str:
    """Strip non-ASCII chars for Windows console."""
    return text.encode("ascii", errors="replace").decode("ascii")


class Dashboard:
    """Rich terminal dashboard for the prediction system."""

    def __init__(self):
        self._console = Console()
        self._db = Database(settings.database_path)
        self._tracker = EvaluationTracker(settings.database_path)
        self._results: list[ResearchResult] = []
        self._scan_results: list = []
        self._last_update: datetime | None = None
        self._cycle_count = 0

    def _build_header(self) -> Panel:
        """System header with status."""
        status = f"Last update: {self._last_update.strftime('%H:%M:%S')}" if self._last_update else "Not yet run"
        text = Text()
        text.append("Tech Event Prediction System v0.5\n", style="bold white")
        text.append("Bayesian + Classical ML + Claude LLM -> Ensemble\n", style="dim")
        text.append(f"Cycle #{self._cycle_count} | {status} | {len(self._results)} questions tracked", style="dim cyan")
        return Panel(text, style="blue", title="Dashboard")

    def _build_edge_table(self) -> Table:
        """Main edge detection table."""
        table = Table(title="Edge Detection", show_lines=True, expand=True)
        table.add_column("Question", max_width=35, style="bold")
        table.add_column("Our Prob", justify="right")
        table.add_column("Market", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("EV", justify="right")
        table.add_column("Kelly", justify="right")
        table.add_column("VPIN", justify="center")
        table.add_column("MC 90% CI", justify="center")

        if not self._results:
            table.add_row("[dim]No results yet. Run analysis first.[/dim]", *[""] * 7)
            return table

        for r in self._results:
            # Edge coloring
            edge = r.edge.edge_pct
            if edge > 0.05:
                edge_str = f"[green]{edge:+.1%}[/green]"
                ev_str = f"[green]${r.edge.expected_value:+.3f}[/green]"
                direction = "[green]YES[/green]"
            elif edge < -0.05:
                edge_str = f"[red]{edge:+.1%}[/red]"
                ev_str = f"[red]${r.edge.expected_value:+.3f}[/red]"
                direction = "[red]NO[/red]"
            else:
                edge_str = f"[yellow]{edge:+.1%}[/yellow]"
                ev_str = f"[yellow]${r.edge.expected_value:+.3f}[/yellow]"
                direction = "[yellow]-[/yellow]"

            # VPIN coloring
            vpin_colors = {"low": "green", "medium": "yellow", "elevated": "red", "high": "bold red"}
            vpin_color = vpin_colors.get(r.vpin_level, "white")

            # MC CI
            ci_lo, ci_hi = r.mc_prob_ci

            table.add_row(
                _safe(r.question.title[:35]),
                f"{r.ensemble.probability:.0%}",
                f"{r.edge.market_prob:.0%}",
                edge_str,
                ev_str,
                f"{r.edge.kelly_fraction:.1%}",
                f"[{vpin_color}]{r.vpin_level}[/{vpin_color}]",
                f"{ci_lo:.0%}-{ci_hi:.0%}",
            )

        return table

    def _build_predictor_detail(self) -> Table:
        """Breakdown of each predictor's output per question."""
        table = Table(title="Predictor Breakdown", show_lines=True, expand=True)
        table.add_column("Question", max_width=20)
        table.add_column("Bayesian", justify="right")
        table.add_column("Classical ML", justify="right")
        table.add_column("Claude LLM", justify="right")
        table.add_column("Ensemble", justify="right", style="bold yellow")

        for r in self._results:
            row = [_safe(r.question.title[:20])]
            for pred in r.predictions:
                row.append(f"{pred.probability:.0%} ({pred.confidence:.0%})")
            # Pad if a predictor failed
            while len(row) < 4:
                row.append("-")
            row.append(f"{r.ensemble.probability:.0%} ({r.ensemble.confidence:.0%})")
            table.add_row(*row)

        if not self._results:
            table.add_row("[dim]No results yet.[/dim]", *[""] * 4)

        return table

    def _build_monte_carlo_table(self) -> Table:
        """Monte Carlo summary per question."""
        table = Table(title="Monte Carlo Analysis", show_lines=True, expand=True)
        table.add_column("Question", max_width=25)
        table.add_column("Prob Mean", justify="right")
        table.add_column("Edge Mean", justify="right")
        table.add_column("Kelly Mean", justify="right")
        table.add_column("CV(edge)", justify="right")

        for r in self._results:
            cv_color = "green" if r.mc_cv_edge < 0.3 else "yellow" if r.mc_cv_edge < 0.6 else "red"
            table.add_row(
                _safe(r.question.title[:25]),
                f"{r.mc_prob_mean:.1%}",
                f"{r.mc_edge_mean:+.1%}",
                f"{r.mc_kelly_mean:.1%}",
                f"[{cv_color}]{r.mc_cv_edge:.2f}[/{cv_color}]",
            )

        if not self._results:
            table.add_row("[dim]No results yet.[/dim]", *[""] * 4)

        return table

    def _build_evaluation_table(self) -> Table:
        """Historical evaluation metrics."""
        table = Table(title="Predictor Evaluation", show_lines=True, expand=True)
        table.add_column("Predictor", style="bold")
        table.add_column("N", justify="right")
        table.add_column("Brier", justify="right")
        table.add_column("Accuracy", justify="right")
        table.add_column("Cum. EV", justify="right")
        table.add_column("ECE", justify="right")

        summaries = compare_predictors(self._tracker)
        if not summaries:
            table.add_row("[dim]No resolved predictions yet.[/dim]", *[""] * 5)
            return table

        for s in summaries:
            ev_color = "green" if s.cumulative_ev > 0 else "red"
            table.add_row(
                s.predictor_name,
                str(s.n_predictions),
                f"{s.brier_score:.4f}",
                f"{s.accuracy:.1%}",
                f"[{ev_color}]${s.cumulative_ev:+.2f}[/{ev_color}]",
                f"{s.expected_calibration_error:.4f}",
            )

        return table

    def _build_scanner_table(self) -> Table:
        """Market scanner results."""
        table = Table(title="Market Scanner (Top Opportunities)", show_lines=True, expand=True)
        table.add_column("#", justify="right", style="dim")
        table.add_column("Market", max_width=40)
        table.add_column("YES", justify="right")
        table.add_column("Entropy", justify="right")
        table.add_column("Volume", justify="right")
        table.add_column("Score", justify="right")

        if not self._scan_results:
            table.add_row("[dim]Run scanner to see results.[/dim]", *[""] * 5)
            return table

        for i, c in enumerate(self._scan_results[:10], 1):
            table.add_row(
                str(i),
                _safe(c.question[:40]),
                f"{c.yes_price:.0%}",
                f"{c.entropy:.3f}",
                f"${c.total_volume:,.0f}",
                f"{c.opportunity_score:.3f}",
            )

        return table

    def render_full(self) -> None:
        """Render the complete dashboard (non-live, single shot)."""
        self._console.clear()
        self._console.print(self._build_header())
        self._console.print()
        self._console.print(self._build_edge_table())
        self._console.print()
        self._console.print(self._build_predictor_detail())
        self._console.print()
        self._console.print(self._build_monte_carlo_table())
        self._console.print()
        self._console.print(self._build_evaluation_table())

        if self._scan_results:
            self._console.print()
            self._console.print(self._build_scanner_table())

    async def run_analysis(self) -> None:
        """Run the full analysis pipeline and update dashboard data."""
        self._cycle_count += 1
        self._console.print("[bold]Running analysis...[/bold]")

        researcher = Researcher(self._db, self._tracker)
        questions = load_questions()

        # Filter expired
        now = datetime.now()
        open_questions = [q for q in questions if q.deadline > now]

        self._results = await researcher.analyze_all(open_questions)
        self._last_update = datetime.now()

        self._console.print(f"[green]Analysis complete: {len(self._results)} questions evaluated.[/green]")

    async def run_scan(self) -> None:
        """Run the market scanner."""
        self._console.print("[bold]Scanning markets...[/bold]")
        candidates = await scan_markets(min_entropy=0.3, min_volume=0, max_results=20)
        self._scan_results = filter_actionable(candidates)
        self._console.print(f"[green]Found {len(self._scan_results)} actionable markets.[/green]")

    async def run_interactive(self) -> None:
        """Interactive dashboard with menu."""
        self._console.print(Panel(
            "[bold]Tech Event Prediction System v0.5[/bold]\n"
            "Interactive Dashboard",
            style="blue",
        ))

        while True:
            self._console.print("\n[bold]Commands:[/bold]")
            self._console.print("  [cyan]1[/cyan] - Run full analysis (all questions)")
            self._console.print("  [cyan]2[/cyan] - Scan for new market opportunities")
            self._console.print("  [cyan]3[/cyan] - Show dashboard")
            self._console.print("  [cyan]4[/cyan] - Start auto-scheduler (runs every 6h)")
            self._console.print("  [cyan]q[/cyan] - Quit")

            try:
                choice = await asyncio.to_thread(input, "\n> ")
            except (EOFError, KeyboardInterrupt):
                break

            if choice == "1":
                await self.run_analysis()
                self.render_full()
            elif choice == "2":
                await self.run_scan()
                self.render_full()
            elif choice == "3":
                self.render_full()
            elif choice == "4":
                await self._run_scheduler()
            elif choice.lower() == "q":
                break
            else:
                self._console.print("[yellow]Unknown command.[/yellow]")

        self._db.close()
        self._console.print("[dim]Dashboard closed.[/dim]")

    async def _run_scheduler(self) -> None:
        """Run the scheduler with dashboard updates."""
        self._console.print("[bold]Starting scheduler (6h interval). Ctrl+C to stop.[/bold]")
        scheduler = Scheduler(interval_hours=6)

        try:
            while True:
                results = await scheduler.run_once()
                self._results = results
                self._last_update = datetime.now()
                self._cycle_count = scheduler.cycle_count
                self.render_full()

                next_time = datetime.now().timestamp() + 6 * 3600
                next_str = datetime.fromtimestamp(next_time).strftime("%H:%M:%S")
                self._console.print(f"\n[dim]Next cycle at {next_str}. Press Ctrl+C to return to menu.[/dim]")

                await asyncio.sleep(6 * 3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._console.print("\n[yellow]Scheduler stopped. Returning to menu.[/yellow]")

    async def run_oneshot(self) -> None:
        """Run analysis once and display dashboard."""
        await self.run_analysis()
        await self.run_scan()
        self.render_full()
