"""Main entry point: predict one question and show edge analysis.

Phase 4: Ensemble predictor, calibration metrics, market selector.
Bayesian + Classical ML + Claude LLM -> Ensemble (weighted combination).
Monte Carlo confidence intervals, empirical Kelly sizing,
evaluation tracking with Brier score, calibration curves, cumulative EV.

Usage:
    python scripts/run_prediction.py
    python scripts/run_prediction.py --question gpt5-release-2026
    python scripts/run_prediction.py --all
    python scripts/run_prediction.py --scan
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Fix Windows Unicode encoding issues
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _safe(text: str) -> str:
    """Strip non-ASCII chars that Windows console can't render."""
    return text.encode("ascii", errors="replace").decode("ascii")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.github_activity import GitHubActivityCollector  # noqa: E402
from src.collectors.news_api import NewsAPICollector  # noqa: E402
from src.collectors.polymarket import PolymarketCollector  # noqa: E402
from src.collectors.registry import CollectorRegistry  # noqa: E402
from src.collectors.rss_feed import RSSFeedCollector  # noqa: E402
from src.config import settings  # noqa: E402
from src.edge.edge_finder import find_edge  # noqa: E402
from src.edge.vpin import assess_vpin, estimate_vpin_from_price_movement  # noqa: E402
from src.evaluation.tracker import EvaluationTracker  # noqa: E402
from src.features.credibility import CredibilityExtractor  # noqa: E402
from src.features.frequency import FrequencyExtractor  # noqa: E402
from src.features.market_features import MarketFeatureExtractor  # noqa: E402
from src.features.pipeline import FeaturePipeline  # noqa: E402
from src.features.sentiment import SentimentExtractor  # noqa: E402
from src.features.temporal import TemporalExtractor  # noqa: E402
from src.edge.market_selector import scan_markets, filter_actionable  # noqa: E402
from src.evaluation.metrics import compare_predictors  # noqa: E402
from src.math.entropy import binary_entropy  # noqa: E402
from src.math.monte_carlo import simulate_edge, simulate_probability  # noqa: E402
from src.models.prediction import PredictionRecord  # noqa: E402
from src.models.question import MarketData, Question  # noqa: E402
from src.predictors.base import BasePredictor  # noqa: E402
from src.predictors.bayesian_predictor import BayesianPredictor  # noqa: E402
from src.predictors.classical_ml import ClassicalMLPredictor  # noqa: E402
from src.predictors.ensemble import EnsemblePredictor, compute_ensemble_weights  # noqa: E402
from src.predictors.llm_predictor import LLMPredictor  # noqa: E402
from src.storage.database import Database  # noqa: E402

console = Console()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def load_questions(yaml_path: str = "data/questions/sample_questions.yaml") -> list[Question]:
    """Load questions from YAML file."""
    path = Path(yaml_path)
    if not path.exists():
        console.print(f"[red]Questions file not found: {yaml_path}[/red]")
        return []

    with open(path) as f:
        data = yaml.safe_load(f)

    questions = []
    for q in data.get("questions", []):
        market_data = q.get("market", {})
        questions.append(
            Question(
                id=q["id"],
                title=q["title"],
                description=q.get("description", ""),
                deadline=datetime.fromisoformat(q["deadline"]),
                tags=q.get("tags", []),
                search_keywords=q.get("search_keywords", []),
                market=MarketData(
                    market_id=market_data.get("market_id", ""),
                    slug=market_data.get("slug", ""),
                ),
            )
        )
    return questions


async def predict_question(question: Question, db: Database, tracker: EvaluationTracker) -> None:
    """Run the full prediction pipeline for one question."""

    console.print(Panel(f"[bold cyan]{question.title}[/bold cyan]", title="Question"))
    console.print(f"  Deadline: {question.deadline.strftime('%Y-%m-%d')}")
    console.print(f"  Tags: {', '.join(question.tags)}")
    console.print(f"  Keywords: {', '.join(question.search_keywords[:3])}")
    console.print()

    # ── Step 1: Collect evidence ──
    console.print("[bold]Step 1: Collecting evidence...[/bold]")
    registry = CollectorRegistry()
    registry.register(RSSFeedCollector())
    registry.register(PolymarketCollector())
    registry.register(NewsAPICollector())       # Phase 3
    registry.register(GitHubActivityCollector())  # Phase 3

    bundle = await registry.collect_all(question)
    console.print(f"  Collected {bundle.item_count} evidence items from {registry.active_collectors}")
    console.print()

    # Try to get live market data
    polymarket = PolymarketCollector()
    market_data = question.market
    if question.market.market_id:
        live_market = await polymarket.get_market_by_id(question.market.market_id)
        if live_market:
            market_data = live_market
            question.market = live_market

    # ── Step 2: Extract features ──
    console.print("[bold]Step 2: Extracting features...[/bold]")
    pipeline = FeaturePipeline()
    pipeline.add(TemporalExtractor(deadline=question.deadline))
    pipeline.add(FrequencyExtractor())
    pipeline.add(SentimentExtractor())
    pipeline.add(CredibilityExtractor())
    pipeline.add(MarketFeatureExtractor(market=market_data))

    features = pipeline.extract_all(bundle)

    # Show evidence table WITH sentiment scores
    if bundle.items:
        evidence_table = Table(title="Top Evidence (with sentiment)", show_lines=True)
        evidence_table.add_column("Source", style="cyan", max_width=15)
        evidence_table.add_column("Title", max_width=42)
        evidence_table.add_column("Sent.", justify="right", max_width=7)
        evidence_table.add_column("Date", max_width=12)

        for item in bundle.items[:10]:
            date_str = item.published_at.strftime("%Y-%m-%d") if item.published_at else "?"
            sent = item.sentiment_score
            if sent > 0.05:
                sent_str = f"[green]+{sent:.2f}[/green]"
            elif sent < -0.05:
                sent_str = f"[red]{sent:.2f}[/red]"
            else:
                sent_str = f"[dim]{sent:.2f}[/dim]"
            evidence_table.add_row(_safe(item.source_name[:15]), _safe(item.title[:42]), sent_str, date_str)
        console.print(evidence_table)
    console.print()

    # Show key features grouped by category
    feature_groups = {
        "Temporal": ["days_to_deadline", "deadline_urgency", "evidence_recency_ratio", "avg_evidence_age_days"],
        "Frequency": ["total_mentions", "mentions_last_7d", "mention_acceleration", "source_diversity"],
        "Sentiment": ["avg_sentiment", "positive_ratio", "negative_ratio", "sentiment_momentum", "credibility_weighted_sentiment"],
        "Credibility": ["avg_credibility", "high_credibility_ratio"],
        "Market": ["market_yes_price", "market_entropy", "market_volume_24h", "volume_intensity"],
    }

    feature_table = Table(title="Features by Category")
    feature_table.add_column("Category", style="bold cyan")
    feature_table.add_column("Feature", style="dim")
    feature_table.add_column("Value", justify="right", style="yellow")

    for category, keys in feature_groups.items():
        first = True
        for key in keys:
            if key in features:
                val = features[key]
                cat_label = category if first else ""
                feature_table.add_row(cat_label, key, f"{val:.3f}")
                first = False

    console.print(feature_table)
    console.print()

    # ── Step 3: VPIN Assessment ──
    console.print("[bold]Step 3: VPIN toxicity check...[/bold]")
    vpin_value = estimate_vpin_from_price_movement(
        current_price=features.get("market_yes_price", 0.5),
        volume_24h=features.get("market_volume_24h", 0),
        price_change_24h=0.0,
    )
    vpin_assessment = assess_vpin(vpin_value)
    vpin_flag = not vpin_assessment["trust_market_price"]

    vpin_color = {"low": "green", "medium": "yellow", "elevated": "red", "high": "bold red"}
    color = vpin_color.get(vpin_assessment["level"], "white")
    console.print(f"  VPIN: [{color}]{vpin_value:.3f} ({vpin_assessment['level']})[/{color}]")
    console.print(f"  {vpin_assessment['description']}")
    if vpin_flag:
        console.print("  [red]WARNING: Market price may not reflect true probability.[/red]")
        console.print(f"  Kelly multiplier: {vpin_assessment['kelly_multiplier']:.0%}")
    console.print()

    # ── Step 4: Run predictors (3 sub-models + ensemble) ──
    console.print("[bold]Step 4: Running predictors...[/bold]")

    sub_predictors: list[BasePredictor] = [
        BayesianPredictor(),
        ClassicalMLPredictor(),
        LLMPredictor(),
    ]

    predictor_colors = {
        "bayesian": "cyan",
        "classical_ml": "green",
        "claude_llm": "magenta",
        "ensemble": "bold yellow",
    }

    predictions = []
    for predictor in sub_predictors:
        console.print(f"  Running {predictor.name}...")
        pred = await predictor.predict(question, bundle, features)
        predictions.append(pred)

        border = predictor_colors.get(predictor.name, "white")
        console.print(Panel(
            pred.reasoning,
            title=f"{predictor.name} (prob={pred.probability:.1%}, conf={pred.confidence:.0%})",
            border_style=border,
        ))
        console.print()

    # Phase 4: Formal ensemble predictor
    console.print("  Running ensemble...")
    learned_weights = compute_ensemble_weights(tracker)
    has_learned = learned_weights != {"bayesian": 0.25, "classical_ml": 0.25, "claude_llm": 0.50}
    strategy = "calibration_weighted" if has_learned else "confidence_weighted"

    ensemble = EnsemblePredictor(strategy=strategy, learned_weights=learned_weights)
    ensemble.set_predictions(predictions)
    ensemble_pred = await ensemble.predict(question, bundle, features)

    console.print(Panel(
        ensemble_pred.reasoning,
        title=f"ENSEMBLE (prob={ensemble_pred.probability:.1%}, conf={ensemble_pred.confidence:.0%})",
        border_style="bold yellow",
    ))
    console.print()

    ensemble_prob = ensemble_pred.probability
    avg_confidence = ensemble_pred.confidence

    # ── Step 5: Compare predictors + Edge analysis ──
    console.print("[bold]Step 5: Comparison & Edge analysis...[/bold]")
    market_price = features.get("market_yes_price", 0.5)

    # Comparison table
    comp_table = Table(title="Predictor Comparison", show_lines=True)
    comp_table.add_column("Predictor", style="bold")
    comp_table.add_column("Probability", justify="right")
    comp_table.add_column("Confidence", justify="right")
    comp_table.add_column("vs Market", justify="right")

    for pred in predictions:
        diff = pred.probability - market_price
        diff_color = "green" if diff > 0.05 else "red" if diff < -0.05 else "yellow"
        comp_table.add_row(
            pred.predictor_name,
            f"{pred.probability:.1%}",
            f"{pred.confidence:.0%}",
            f"[{diff_color}]{diff:+.1%}[/{diff_color}]",
        )

    # Add ensemble row
    ens_diff = ensemble_prob - market_price
    ens_color = "green" if ens_diff > 0.05 else "red" if ens_diff < -0.05 else "yellow"
    comp_table.add_row(
        "[bold yellow]ENSEMBLE[/bold yellow]",
        f"[bold]{ensemble_prob:.1%}[/bold]",
        f"{avg_confidence:.0%}",
        f"[bold {ens_color}]{ens_diff:+.1%}[/bold {ens_color}]",
    )

    comp_table.add_row(
        "[dim]Market (crowd)[/dim]",
        f"[dim]{market_price:.1%}[/dim]",
        "[dim]-[/dim]",
        "[dim]+0.0%[/dim]",
    )
    console.print(comp_table)
    console.print()

    edge = find_edge(
        question_id=question.id,
        model_prob=ensemble_prob,
        market_price_yes=market_price,
        confidence=avg_confidence,
        vpin_flag=vpin_flag,
    )

    # ── Step 6: Monte Carlo confidence intervals (Phase 3) ──
    console.print("[bold]Step 6: Monte Carlo analysis (10,000 simulations)...[/bold]")

    mc_result = simulate_probability(
        base_probability=ensemble_prob,
        confidence=avg_confidence,
        n_simulations=10_000,
    )

    edge_dist = simulate_edge(
        model_probability=ensemble_prob,
        model_confidence=avg_confidence,
        market_price=market_price,
        n_simulations=10_000,
    )

    mc_table = Table(title="Monte Carlo Results", show_lines=True)
    mc_table.add_column("Metric", style="bold")
    mc_table.add_column("Value", justify="right")

    mc_table.add_row("Probability Mean", f"{mc_result.mean:.1%}")
    mc_table.add_row("Probability Median", f"{mc_result.median:.1%}")
    mc_table.add_row("90% CI", f"[{mc_result.ci_5:.1%}, {mc_result.ci_95:.1%}]")
    mc_table.add_row("Std Dev", f"{mc_result.std:.3f}")
    mc_table.add_row("", "")
    mc_table.add_row("Edge Mean", f"{edge_dist.mean_edge:+.1%}")
    mc_table.add_row("P(positive edge)", f"{edge_dist.prob_positive_edge:.0%}")
    mc_table.add_row("P(positive EV)", f"{edge_dist.prob_positive_ev:.0%}")
    mc_table.add_row("", "")
    mc_table.add_row("Empirical Kelly (mean)", f"{edge_dist.mean_kelly:.1%}")
    mc_table.add_row("Empirical Kelly (median)", f"{edge_dist.median_kelly:.1%}")
    mc_table.add_row("Kelly 90% CI", f"[{edge_dist.kelly_ci_5:.1%}, {edge_dist.kelly_ci_95:.1%}]")
    mc_table.add_row("CV(edge)", f"{edge_dist.cv_edge:.2f}")

    console.print(mc_table)
    console.print()

    # Edge analysis table
    edge_table = Table(title="Edge Analysis", show_lines=True)
    edge_table.add_column("Metric", style="bold")
    edge_table.add_column("Value", justify="right")

    edge_table.add_row("Ensemble Probability", f"{ensemble_prob:.1%}")
    edge_table.add_row("Market Probability", f"{market_price:.1%}")

    edge_color = "green" if edge.edge_pct > 0.05 else "red" if edge.edge_pct < -0.05 else "yellow"
    edge_table.add_row("Edge", f"[{edge_color}]{edge.edge_pct:+.1%}[/{edge_color}]")
    edge_table.add_row("Expected Value", f"[{edge_color}]${edge.expected_value:+.4f}[/{edge_color}]")
    edge_table.add_row("Kelly Fraction", f"{edge.kelly_fraction:.1%}")
    edge_table.add_row("Direction", edge.direction)
    edge_table.add_row("VPIN Status", f"[{color}]{vpin_assessment['level']}[/{color}]")
    edge_table.add_row("Entropy", f"{binary_entropy(market_price):.3f} bits")

    console.print(edge_table)

    if edge.has_edge:
        console.print(
            f"\n[bold green]EDGE DETECTED:[/bold green] {edge.direction} "
            f"(EV: ${edge.expected_value:+.4f}, Kelly: {edge.kelly_fraction:.1%})"
        )
    else:
        console.print("\n[yellow]No significant edge detected.[/yellow]")

    # ── Store results + evaluation tracking ──
    record = PredictionRecord(
        question_id=question.id,
        predictions=predictions,
        ensemble_probability=ensemble_prob,
        edge=edge,
    )
    db.store_question(question)
    db.store_evidence(bundle)
    db.store_prediction(record)
    db.store_market_snapshot(question.id, market_data)

    # Phase 3: Store evaluation snapshots for each predictor
    for pred in predictions:
        tracker.store_snapshot(
            question_id=question.id,
            predictor_name=pred.predictor_name,
            probability=pred.probability,
            confidence=pred.confidence,
            market_price=market_price,
            edge_pct=pred.probability - market_price,
            kelly_fraction=edge.kelly_fraction,
            evidence_count=pred.evidence_count,
            features=features,
        )

    # Also store ensemble snapshot
    tracker.store_snapshot(
        question_id=question.id,
        predictor_name="ensemble",
        probability=ensemble_prob,
        confidence=avg_confidence,
        market_price=market_price,
        edge_pct=edge.edge_pct,
        kelly_fraction=edge.kelly_fraction,
        evidence_count=bundle.item_count,
        features=features,
    )

    console.print("\n[dim]Results stored to database + evaluation tracker.[/dim]")


async def run_market_scan():
    """Scan Polymarket for high-opportunity tech markets."""
    console.print(Panel("[bold]Market Scanner[/bold]\nFinding high-opportunity tech markets on Polymarket...", style="blue"))

    candidates = await scan_markets(min_entropy=0.3, min_volume=0, max_results=30)
    actionable = filter_actionable(candidates)

    if not candidates:
        console.print("[yellow]No markets found. Polymarket API may be slow.[/yellow]")
        return

    scan_table = Table(title=f"Top Markets by Opportunity ({len(actionable)} actionable / {len(candidates)} total)", show_lines=True)
    scan_table.add_column("#", justify="right", style="dim")
    scan_table.add_column("Question", max_width=50)
    scan_table.add_column("YES", justify="right")
    scan_table.add_column("Entropy", justify="right")
    scan_table.add_column("Volume", justify="right")
    scan_table.add_column("Score", justify="right")

    for i, c in enumerate(candidates[:20], 1):
        is_actionable = c in actionable
        style = "" if is_actionable else "dim"
        scan_table.add_row(
            str(i),
            f"[{style}]{_safe(c.question[:50])}[/{style}]" if style else _safe(c.question[:50]),
            f"{c.yes_price:.0%}",
            f"{c.entropy:.3f}",
            f"${c.total_volume:,.0f}",
            f"{c.opportunity_score:.3f}",
        )

    console.print(scan_table)
    console.print("\n[dim]Actionable = entropy > 0.5, price 10-90%, > 7 days to expiry[/dim]")


async def show_evaluation(tracker: EvaluationTracker):
    """Show evaluation metrics for all predictors."""
    summaries = compare_predictors(tracker)

    if not summaries:
        console.print("[yellow]No resolved predictions yet. Metrics will appear after market resolutions.[/yellow]")
        return

    eval_table = Table(title="Predictor Evaluation (resolved markets)", show_lines=True)
    eval_table.add_column("Predictor", style="bold")
    eval_table.add_column("N", justify="right")
    eval_table.add_column("Brier", justify="right")
    eval_table.add_column("Log Loss", justify="right")
    eval_table.add_column("Accuracy", justify="right")
    eval_table.add_column("Avg Edge", justify="right")
    eval_table.add_column("Cum. EV", justify="right")
    eval_table.add_column("ECE", justify="right")

    for s in summaries:
        ev_color = "green" if s.cumulative_ev > 0 else "red"
        eval_table.add_row(
            s.predictor_name,
            str(s.n_predictions),
            f"{s.brier_score:.4f}",
            f"{s.log_loss:.4f}",
            f"{s.accuracy:.1%}",
            f"{s.avg_edge:+.1%}",
            f"[{ev_color}]${s.cumulative_ev:+.2f}[/{ev_color}]",
            f"{s.expected_calibration_error:.4f}",
        )

    console.print(eval_table)


async def main():
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("scripts/run_prediction.py")
    # Parse args
    question_id = None
    run_all = False
    run_scan = False
    if len(sys.argv) > 1:
        if sys.argv[1] == "--all":
            run_all = True
        elif sys.argv[1] == "--scan":
            run_scan = True
        elif sys.argv[1] == "--question" and len(sys.argv) > 2:
            question_id = sys.argv[2]

    if run_scan:
        await run_market_scan()
        return

    # Load questions
    questions = load_questions()
    if not questions:
        console.print("[red]No questions found. Check data/questions/sample_questions.yaml[/red]")
        return

    # Filter to specific question if requested
    if question_id:
        questions = [q for q in questions if q.id == question_id]
        if not questions:
            console.print(f"[red]Question '{question_id}' not found.[/red]")
            return

    db = Database(settings.database_path)
    tracker = EvaluationTracker(settings.database_path)

    console.print(
        Panel(
            "[bold]Tech Event Prediction System v0.4[/bold]\n"
            "Bayesian + Classical ML + Claude LLM -> Ensemble\n"
            "Monte Carlo | Empirical Kelly | Calibration Metrics\n"
            "Sentiment | VPIN | Edge detection | Backtesting",
            style="blue",
        )
    )
    console.print()

    if run_all:
        for question in questions:
            await predict_question(question, db, tracker)
            console.print("\n" + "=" * 70 + "\n")
    else:
        await predict_question(questions[0], db, tracker)

        if len(questions) > 1:
            console.print("\n[bold]Other available questions:[/bold]")
            for q in questions[1:]:
                console.print(f"  * {q.id}: {q.title}")
            console.print(
                "\n[dim]Run with --question <id> or --all to predict more.[/dim]"
            )

    # Phase 4: Show evaluation metrics
    console.print("\n")
    await show_evaluation(tracker)

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
