"""Level 4 Predictor: Weighted ensemble of all predictors."""

import logging

from src.models.evidence import EvidenceBundle
from src.models.prediction import Prediction
from src.models.question import Question
from src.predictors.base import BasePredictor

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "bayesian": 0.25,
    "classical_ml": 0.25,
    "claude_llm": 0.50,
}


class EnsemblePredictor(BasePredictor):
    """Weighted combination of multiple predictors."""

    name = "ensemble"

    def __init__(
        self,
        strategy: str = "confidence_weighted",
        learned_weights: dict[str, float] | None = None,
    ):
        self._strategy = strategy
        self._learned_weights = learned_weights or {}
        self._sub_predictions: list[Prediction] = []

    def set_predictions(self, predictions: list[Prediction]) -> None:
        """Set the sub-predictor results to combine."""
        self._sub_predictions = predictions

    def _confidence_weighted(self) -> tuple[float, float, str]:
        """Weight by each predictor's self-reported confidence."""
        valid = [p for p in self._sub_predictions if p.confidence > 0]
        if not valid:
            return 0.5, 0.0, "No valid predictions to ensemble."

        total_weight = sum(p.confidence for p in valid)
        prob = sum(p.probability * p.confidence for p in valid) / total_weight
        avg_conf = total_weight / len(valid)

        parts = ["Confidence-weighted ensemble:"]
        for p in sorted(valid, key=lambda x: x.confidence, reverse=True):
            w = p.confidence / total_weight
            parts.append(
                f"  {p.predictor_name}: {p.probability:.1%} "
                f"(conf={p.confidence:.0%}, weight={w:.0%})"
            )
        parts.append(f"Combined: {prob:.1%}")

        return prob, avg_conf, "\n".join(parts)

    def _calibration_weighted(self) -> tuple[float, float, str]:
        """Weight by historical calibration performance (inverse Brier score)."""
        weights = self._learned_weights or DEFAULT_WEIGHTS
        valid = [p for p in self._sub_predictions if p.predictor_name in weights]

        if not valid:
            return self._confidence_weighted()

        total_weight = sum(weights.get(p.predictor_name, 0.25) for p in valid)
        if total_weight == 0:
            return self._confidence_weighted()

        prob = sum(
            p.probability * weights.get(p.predictor_name, 0.25)
            for p in valid
        ) / total_weight

        avg_conf = sum(p.confidence for p in valid) / len(valid)

        parts = ["Calibration-weighted ensemble:"]
        for p in valid:
            w = weights.get(p.predictor_name, 0.25) / total_weight
            parts.append(
                f"  {p.predictor_name}: {p.probability:.1%} "
                f"(brier_weight={w:.0%})"
            )
        parts.append(f"Combined: {prob:.1%}")

        return prob, avg_conf, "\n".join(parts)

    def _equal_weighted(self) -> tuple[float, float, str]:
        """Simple average of all predictors."""
        valid = [p for p in self._sub_predictions if p.confidence > 0]
        if not valid:
            return 0.5, 0.0, "No valid predictions to ensemble."

        prob = sum(p.probability for p in valid) / len(valid)
        avg_conf = sum(p.confidence for p in valid) / len(valid)

        parts = ["Equal-weighted ensemble:"]
        for p in valid:
            parts.append(f"  {p.predictor_name}: {p.probability:.1%}")
        parts.append(f"Combined: {prob:.1%}")

        return prob, avg_conf, "\n".join(parts)

    async def predict(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> Prediction:
        if not self._sub_predictions:
            return Prediction(
                question_id=question.id,
                predictor_name=self.name,
                probability=0.5,
                confidence=0.0,
                reasoning="No sub-predictions provided.",
                evidence_count=bundle.item_count,
            )

        if self._strategy == "calibration_weighted" and self._learned_weights:
            prob, conf, reasoning = self._calibration_weighted()
        elif self._strategy == "equal":
            prob, conf, reasoning = self._equal_weighted()
        else:
            prob, conf, reasoning = self._confidence_weighted()

        return Prediction(
            question_id=question.id,
            predictor_name=self.name,
            probability=round(max(0.02, min(0.98, prob)), 3),
            confidence=round(conf, 2),
            reasoning=reasoning,
            evidence_count=bundle.item_count,
        )


def compute_ensemble_weights(
    tracker,
    predictor_names: list[str] | None = None,
    min_predictions: int = 10,
) -> dict[str, float]:
    """Compute optimal weights from historical calibration performance."""
    resolved = tracker.get_resolved_predictions()
    if len(resolved) < min_predictions:
        logger.info(
            f"Only {len(resolved)} resolved predictions "
            f"(need {min_predictions}). Using default weights."
        )
        return DEFAULT_WEIGHTS.copy()

    # Group by predictor
    by_predictor: dict[str, list[tuple]] = {}
    for snapshot, outcome in resolved:
        name = snapshot.predictor_name
        if name == "ensemble":
            continue
        if predictor_names and name not in predictor_names:
            continue
        by_predictor.setdefault(name, []).append((snapshot, outcome))

    # Compute Brier score for each predictor
    brier_scores: dict[str, float] = {}
    for name, pairs in by_predictor.items():
        if len(pairs) < 3:
            continue
        brier = sum(
            (s.probability - o) ** 2 for s, o in pairs
        ) / len(pairs)
        brier_scores[name] = brier

    if not brier_scores:
        return DEFAULT_WEIGHTS.copy()

    inv_brier = {name: 1.0 / (brier + 0.01) for name, brier in brier_scores.items()}
    total = sum(inv_brier.values())
    weights = {name: v / total for name, v in inv_brier.items()}

    logger.info(f"Learned ensemble weights: {weights}")
    return weights
