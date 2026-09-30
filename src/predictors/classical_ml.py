"""Level 2 Predictor: Gradient-boosted trees (XGBoost + LightGBM).

This is the "workhorse" predictor that top Polymarket bots use.
Gradient-boosted trees are the gold standard for tabular data — they
handle feature interactions, nonlinearities, and missing values natively.

The approach:
1. Extract features from evidence (sentiment, frequency, temporal, market, credibility)
2. Train on historical predictions with known outcomes
3. Use CalibratedClassifierCV for proper probability calibration
4. Ensemble XGBoost + LightGBM for robustness

Until we have enough historical data to train on (Phase 4+), this predictor
uses a heuristic feature-scoring approach that mimics what a trained model
would learn, while providing the infrastructure for real training later.

Key ML concepts used:
- Gradient Boosting: builds trees sequentially, each correcting the previous
- Calibration: transforms raw scores into true probabilities (Platt scaling / isotonic)
- Ensemble: average of XGBoost + LightGBM reduces variance
"""

import logging

from src.models.evidence import EvidenceBundle
from src.models.prediction import Prediction
from src.models.question import Question
from src.predictors.base import BasePredictor

logger = logging.getLogger(__name__)

# Feature weights learned from domain knowledge
# Positive weight = feature increases YES probability
# These serve as initial weights before we have training data
FEATURE_WEIGHTS: dict[str, float] = {
    # Sentiment features (positive sentiment -> higher YES probability)
    "avg_sentiment": 0.15,
    "sentiment_momentum": 0.10,
    "credibility_weighted_sentiment": 0.20,
    "positive_ratio": 0.08,
    "negative_ratio": -0.08,

    # Frequency features (more mentions -> something is happening)
    "mention_acceleration": 0.12,
    "source_diversity": 0.05,

    # Temporal features
    "deadline_urgency": 0.05,
    "evidence_recency_ratio": 0.10,

    # Market features (these anchor toward market consensus)
    "volume_intensity": 0.03,

    # Credibility (high credibility sources saying positive things)
    "avg_credibility": 0.04,
}

# Features that should be scaled to [0, 1] for consistent weighting
SCALE_FEATURES = {
    "avg_sentiment": (-1.0, 1.0),
    "sentiment_momentum": (-1.0, 1.0),
    "credibility_weighted_sentiment": (-1.0, 1.0),
    "positive_ratio": (0.0, 1.0),
    "negative_ratio": (0.0, 1.0),
    "mention_acceleration": (0.0, 5.0),
    "source_diversity": (0.0, 10.0),
    "deadline_urgency": (0.0, 1.0),
    "evidence_recency_ratio": (0.0, 1.0),
    "volume_intensity": (0.0, 5.0),
    "avg_credibility": (0.0, 1.0),
}


def _scale(value: float, min_val: float, max_val: float) -> float:
    """Scale a value to [0, 1] range."""
    if max_val <= min_val:
        return 0.5
    return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))


class ClassicalMLPredictor(BasePredictor):
    """Gradient-boosted tree predictor (heuristic mode until trained).

    In heuristic mode: uses domain-knowledge feature weights to produce
    a probability estimate. This mimics what XGBoost/LightGBM would learn
    from training data, giving us the infrastructure now.

    In trained mode (Phase 4+): loads a trained model from disk and
    predicts using actual learned weights.
    """

    name = "classical_ml"

    def __init__(self):
        self._model = None  # Will hold trained model in Phase 4+
        self._is_trained = False

    def _heuristic_predict(self, features: dict[str, float], market_price: float) -> tuple[float, str]:
        """Feature-weighted prediction (pre-training mode).

        Uses domain knowledge weights to combine features into a probability
        adjustment from the market price. The market price is the anchor —
        we only deviate based on feature evidence.

        Returns (probability, reasoning_string).
        """
        # Start from market price as the anchor
        adjustments: list[tuple[str, float]] = []
        total_adjustment = 0.0

        for feature_name, weight in FEATURE_WEIGHTS.items():
            if feature_name not in features:
                continue

            raw_value = features[feature_name]
            scale_range = SCALE_FEATURES.get(feature_name, (0.0, 1.0))
            scaled = _scale(raw_value, scale_range[0], scale_range[1])

            # Center around 0.5 so neutral features don't shift the probability
            centered = scaled - 0.5
            adjustment = centered * weight

            if abs(adjustment) > 0.001:
                adjustments.append((feature_name, adjustment))
                total_adjustment += adjustment

        # Apply adjustment to market price using sigmoid-like bounding
        # This prevents extreme probabilities from feature noise
        raw_prob = market_price + total_adjustment
        probability = max(0.05, min(0.95, raw_prob))

        # Build reasoning
        reasoning_parts = [f"Market anchor: {market_price:.1%}"]

        # Sort adjustments by magnitude
        adjustments.sort(key=lambda x: abs(x[1]), reverse=True)
        for feat, adj in adjustments[:8]:
            direction = "+" if adj > 0 else "-"
            reasoning_parts.append(
                f"  [{direction}] {feat}: {adj:+.3f} (raw={features.get(feat, 0):.3f})"
            )

        reasoning_parts.append(f"Total adjustment: {total_adjustment:+.3f}")
        reasoning_parts.append(f"Final: {probability:.1%}")
        reasoning_parts.append("[heuristic mode - no training data yet]")

        return probability, "\n".join(reasoning_parts)

    def _trained_predict(self, features: dict[str, float]) -> tuple[float, str]:
        """Predict using a trained XGBoost/LightGBM model.

        This will be implemented in Phase 4 when we have training data.
        """
        # Placeholder for Phase 4. Would be:
        #   feature_names = sorted(FEATURE_WEIGHTS.keys())
        #   x = np.array([[features.get(f, 0.0) for f in feature_names]])
        #   probability = self._model.predict_proba(x)[0][1]
        raise NotImplementedError("Trained prediction requires Phase 4 training data")

    async def predict(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> Prediction:
        market_price = features.get("market_yes_price", 0.5)

        if self._is_trained and self._model is not None:
            try:
                probability, reasoning = self._trained_predict(features)
            except NotImplementedError:
                probability, reasoning = self._heuristic_predict(features, market_price)
        else:
            probability, reasoning = self._heuristic_predict(features, market_price)

        # Confidence: lower in heuristic mode, higher when trained
        if self._is_trained:
            confidence = 0.7  # Trained model has learned patterns
        else:
            # Heuristic confidence based on evidence quantity and feature coverage
            feature_coverage = sum(1 for f in FEATURE_WEIGHTS if f in features)
            coverage_ratio = feature_coverage / max(len(FEATURE_WEIGHTS), 1)
            evidence_factor = min(1.0, bundle.item_count / 15)
            confidence = round(0.3 * coverage_ratio + 0.2 * evidence_factor, 2)

        return Prediction(
            question_id=question.id,
            predictor_name=self.name,
            probability=round(probability, 3),
            confidence=confidence,
            reasoning=reasoning,
            evidence_count=bundle.item_count,
        )
