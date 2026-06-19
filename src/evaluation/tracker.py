"""Evaluation tracker — persistent prediction and edge history.

Stores snapshots of predictions over time so we can:
1. Track how our estimates evolve as new evidence arrives
2. Compare predictor accuracy after market resolution
3. Build calibration curves (do our 70% predictions happen 70% of the time?)
4. Evaluate edge signal quality (did our detected edges actually pay off?)

This is the foundation for Phase 4's full evaluation pipeline.
"""

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class PredictionSnapshot:
    """A point-in-time record of a prediction."""

    question_id: str
    timestamp: str  # ISO format
    predictor_name: str
    probability: float
    confidence: float
    market_price: float
    edge_pct: float
    kelly_fraction: float
    evidence_count: int
    features_json: str  # JSON-encoded feature dict


@dataclass
class CalibrationBucket:
    """Aggregated stats for one calibration bucket (e.g., 60-70% predictions)."""

    bucket_lower: float
    bucket_upper: float
    predicted_mean: float
    actual_rate: float
    count: int


class EvaluationTracker:
    """Stores and retrieves prediction history for evaluation."""

    def __init__(self, db_path: str = "data/predictions.db"):
        self._db_path = db_path
        self._init_tables()

    def _init_tables(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prediction_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    predictor_name TEXT NOT NULL,
                    probability REAL NOT NULL,
                    confidence REAL NOT NULL,
                    market_price REAL NOT NULL,
                    edge_pct REAL NOT NULL,
                    kelly_fraction REAL NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    features_json TEXT,
                    UNIQUE(question_id, timestamp, predictor_name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resolutions (
                    question_id TEXT PRIMARY KEY,
                    resolved_at TEXT NOT NULL,
                    outcome INTEGER NOT NULL,
                    final_market_price REAL
                )
            """)

    def store_snapshot(
        self,
        question_id: str,
        predictor_name: str,
        probability: float,
        confidence: float,
        market_price: float,
        edge_pct: float,
        kelly_fraction: float,
        evidence_count: int,
        features: dict[str, float] | None = None,
    ) -> None:
        """Store a prediction snapshot."""
        timestamp = datetime.now().isoformat()
        features_json = json.dumps(features) if features else "{}"

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO prediction_snapshots
                   (question_id, timestamp, predictor_name, probability,
                    confidence, market_price, edge_pct, kelly_fraction,
                    evidence_count, features_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    question_id, timestamp, predictor_name, probability,
                    confidence, market_price, edge_pct, kelly_fraction,
                    evidence_count, features_json,
                ),
            )

    def store_resolution(
        self,
        question_id: str,
        outcome: int,
        final_market_price: float | None = None,
    ) -> None:
        """Record that a question resolved (outcome: 1=YES, 0=NO)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO resolutions
                   (question_id, resolved_at, outcome, final_market_price)
                   VALUES (?, ?, ?, ?)""",
                (question_id, datetime.now().isoformat(), outcome, final_market_price),
            )

    def get_snapshots(
        self,
        question_id: str | None = None,
        predictor_name: str | None = None,
    ) -> list[PredictionSnapshot]:
        """Retrieve prediction snapshots, optionally filtered."""
        query = "SELECT * FROM prediction_snapshots WHERE 1=1"
        params: list = []

        if question_id:
            query += " AND question_id = ?"
            params.append(question_id)
        if predictor_name:
            query += " AND predictor_name = ?"
            params.append(predictor_name)

        query += " ORDER BY timestamp DESC"

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            PredictionSnapshot(
                question_id=row[1],
                timestamp=row[2],
                predictor_name=row[3],
                probability=row[4],
                confidence=row[5],
                market_price=row[6],
                edge_pct=row[7],
                kelly_fraction=row[8],
                evidence_count=row[9],
                features_json=row[10] or "{}",
            )
            for row in rows
        ]

    def get_resolved_predictions(self) -> list[tuple[PredictionSnapshot, int]]:
        """Get predictions for questions that have resolved.

        Returns list of (snapshot, outcome) tuples where outcome is 0 or 1.
        Only returns the LATEST snapshot for each (question, predictor) pair.
        """
        query = """
            SELECT ps.*, r.outcome
            FROM prediction_snapshots ps
            JOIN resolutions r ON ps.question_id = r.question_id
            WHERE ps.id IN (
                SELECT MAX(id) FROM prediction_snapshots
                GROUP BY question_id, predictor_name
            )
            ORDER BY ps.question_id, ps.predictor_name
        """

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(query).fetchall()

        results = []
        for row in rows:
            snapshot = PredictionSnapshot(
                question_id=row[1],
                timestamp=row[2],
                predictor_name=row[3],
                probability=row[4],
                confidence=row[5],
                market_price=row[6],
                edge_pct=row[7],
                kelly_fraction=row[8],
                evidence_count=row[9],
                features_json=row[10] or "{}",
            )
            outcome = row[11]
            results.append((snapshot, outcome))

        return results

    def compute_calibration(
        self, predictor_name: str | None = None, n_buckets: int = 5
    ) -> list[CalibrationBucket]:
        """Compute calibration buckets for resolved predictions.

        Groups predictions by probability range and compares predicted
        probability against actual outcome rate.

        Perfect calibration: predicted 70% -> actually happens 70% of the time.
        """
        resolved = self.get_resolved_predictions()

        if predictor_name:
            resolved = [(s, o) for s, o in resolved if s.predictor_name == predictor_name]

        if not resolved:
            return []

        bucket_size = 1.0 / n_buckets
        buckets: list[CalibrationBucket] = []

        for i in range(n_buckets):
            lower = i * bucket_size
            upper = (i + 1) * bucket_size

            in_bucket = [
                (s, o) for s, o in resolved
                if lower <= s.probability < upper or (i == n_buckets - 1 and s.probability == upper)
            ]

            if in_bucket:
                predicted_mean = sum(s.probability for s, _ in in_bucket) / len(in_bucket)
                actual_rate = sum(o for _, o in in_bucket) / len(in_bucket)
            else:
                predicted_mean = (lower + upper) / 2
                actual_rate = 0.0

            buckets.append(
                CalibrationBucket(
                    bucket_lower=lower,
                    bucket_upper=upper,
                    predicted_mean=predicted_mean,
                    actual_rate=actual_rate,
                    count=len(in_bucket),
                )
            )

        return buckets

    def summary_stats(self, predictor_name: str | None = None) -> dict:
        """Get summary statistics for a predictor."""
        resolved = self.get_resolved_predictions()

        if predictor_name:
            resolved = [(s, o) for s, o in resolved if s.predictor_name == predictor_name]

        if not resolved:
            return {"total_predictions": 0, "message": "No resolved predictions yet."}

        predictions = [s.probability for s, _ in resolved]
        outcomes = [o for _, o in resolved]
        edges = [s.edge_pct for s, _ in resolved]

        # Brier score: mean squared error of probability estimates
        brier = sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(resolved)

        # Average edge
        avg_edge = sum(edges) / len(edges)

        # Win rate: did we predict the right side?
        correct = sum(
            1 for s, o in resolved
            if (s.probability > 0.5 and o == 1) or (s.probability < 0.5 and o == 0)
        )

        return {
            "total_predictions": len(resolved),
            "brier_score": round(brier, 4),
            "avg_edge": round(avg_edge, 4),
            "win_rate": round(correct / len(resolved), 4),
            "avg_probability": round(sum(predictions) / len(predictions), 4),
            "avg_outcome": round(sum(outcomes) / len(outcomes), 4),
        }
