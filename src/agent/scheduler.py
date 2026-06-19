"""Scheduler — periodic re-evaluation of open questions.

Runs the researcher on all tracked questions at a configurable interval.
Each cycle:
1. Load all open questions
2. Run the full pipeline on each
3. Log results and edge changes
4. Sleep until next cycle

Usage:
    scheduler = Scheduler(interval_hours=6)
    await scheduler.run()  # Runs forever
    await scheduler.run_once()  # Single cycle
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import yaml

from src.agent.researcher import Researcher, ResearchResult
from src.config import settings
from src.evaluation.tracker import EvaluationTracker
from src.models.question import MarketData, Question
from src.storage.database import Database

logger = logging.getLogger(__name__)


def load_questions(yaml_path: str = "data/questions/sample_questions.yaml") -> list[Question]:
    """Load questions from YAML file."""
    path = Path(yaml_path)
    if not path.exists():
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


class Scheduler:
    """Periodically re-evaluates all open questions."""

    def __init__(
        self,
        interval_hours: float = 6,
        questions_path: str = "data/questions/sample_questions.yaml",
    ):
        self._interval = interval_hours * 3600  # Convert to seconds
        self._questions_path = questions_path
        self._running = False
        self._cycle_count = 0
        self._last_results: list[ResearchResult] = []

    @property
    def last_results(self) -> list[ResearchResult]:
        """Results from the most recent cycle."""
        return self._last_results

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self) -> list[ResearchResult]:
        """Run a single evaluation cycle."""
        self._cycle_count += 1
        cycle_start = datetime.now()
        logger.info(f"=== Cycle {self._cycle_count} starting at {cycle_start.strftime('%H:%M:%S')} ===")

        questions = load_questions(self._questions_path)
        if not questions:
            logger.warning("No questions loaded.")
            return []

        # Filter to questions that haven't expired
        now = datetime.now()
        open_questions = [q for q in questions if q.deadline > now]
        logger.info(f"Evaluating {len(open_questions)} open questions (of {len(questions)} total)")

        db = Database(settings.database_path)
        tracker = EvaluationTracker(settings.database_path)
        researcher = Researcher(db, tracker)

        results = await researcher.analyze_all(open_questions)
        self._last_results = results

        # Log summary
        edge_count = sum(1 for r in results if r.has_edge)
        elapsed = (datetime.now() - cycle_start).total_seconds()
        logger.info(
            f"=== Cycle {self._cycle_count} complete: "
            f"{len(results)} analyzed, {edge_count} edges detected, "
            f"{elapsed:.1f}s elapsed ==="
        )

        for r in results:
            if r.has_edge:
                logger.info(f"  EDGE: {r.summary}")

        db.close()
        return results

    async def run(self) -> None:
        """Run evaluation cycles indefinitely."""
        self._running = True
        logger.info(
            f"Scheduler started. Interval: {self._interval / 3600:.1f}h. "
            f"Press Ctrl+C to stop."
        )

        try:
            while self._running:
                await self.run_once()

                next_run = datetime.now().timestamp() + self._interval
                next_time = datetime.fromtimestamp(next_run).strftime("%H:%M:%S")
                logger.info(f"Next cycle at {next_time}")

                # Sleep in small increments so we can be interrupted
                remaining = self._interval
                while remaining > 0 and self._running:
                    sleep_time = min(remaining, 10)
                    await asyncio.sleep(sleep_time)
                    remaining -= sleep_time
        except asyncio.CancelledError:
            logger.info("Scheduler cancelled.")
        finally:
            self._running = False
            logger.info("Scheduler stopped.")

    def stop(self) -> None:
        """Signal the scheduler to stop after the current cycle."""
        self._running = False
