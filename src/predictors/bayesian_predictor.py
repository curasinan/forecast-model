"""Level 1 Predictor: Bayesian updating with evidence-driven likelihood estimation.

This is the foundational predictor. Instead of arbitrary rules, it uses
Bayes' theorem to update a prior probability based on each piece of evidence.

The approach:
1. Start with a prior (market price or 0.5 if no market data)
2. For each evidence item, estimate likelihood ratios based on features
3. Apply Bayesian updating sequentially
4. The final posterior is our probability estimate

This is transparent, interpretable, and educational — you can trace
exactly how each piece of evidence shifted the probability.
"""

from src.math.bayesian import BayesianUpdater
from src.models.evidence import EvidenceBundle
from src.models.prediction import Prediction
from src.models.question import Question
from src.predictors.base import BasePredictor


class BayesianPredictor(BasePredictor):
    """Bayesian updating predictor — the core mental model."""

    name = "bayesian"

    def _estimate_likelihoods(
        self,
        features: dict[str, float],
        bundle: EvidenceBundle,
    ) -> list[tuple[str, float, float]]:
        """Estimate likelihood ratios from features.

        Returns list of (description, P(evidence|YES), P(evidence|NO)).

        These are heuristic estimates — they get refined with calibration data
        over time. The key insight is that even rough Bayesian updating beats
        arbitrary scoring rules because it handles evidence combination correctly.
        """
        updates: list[tuple[str, float, float]] = []

        # Evidence volume signal
        total = features.get("total_mentions", 0)
        if total > 20:
            # High mention count = something is happening = slightly favors YES
            updates.append(("High mention volume", 0.65, 0.45))
        elif total > 5:
            updates.append(("Moderate mention volume", 0.55, 0.50))
        elif total == 0:
            # No evidence at all = slightly favors NO (nothing happening)
            updates.append(("No evidence found", 0.35, 0.55))

        # Acceleration signal
        accel = features.get("mention_acceleration", 0)
        if accel > 2.0:
            updates.append(("Strong mention acceleration", 0.75, 0.35))
        elif accel > 1.2:
            updates.append(("Moderate acceleration", 0.60, 0.45))
        elif accel < 0.5 and total > 5:
            updates.append(("Decelerating mentions", 0.40, 0.55))

        # Deadline proximity
        urgency = features.get("deadline_urgency", 0)
        if urgency > 0.9:
            # Very close to deadline — strong signal either way
            recent = features.get("mentions_last_7d", 0)
            if recent > 10:
                updates.append(("High activity near deadline", 0.70, 0.30))
            else:
                updates.append(("Low activity near deadline", 0.25, 0.65))

        # Source diversity
        diversity = features.get("source_diversity", 0)
        if diversity >= 4:
            updates.append(("Wide source diversity", 0.65, 0.45))

        # Market volume intensity (if available)
        vol_intensity = features.get("volume_intensity", 0)
        if vol_intensity > 1.0:
            updates.append(("High market trading intensity", 0.60, 0.50))

        return updates

    async def predict(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> Prediction:
        # Start with market price as prior (the crowd's best estimate)
        # If no market data, start at 0.5 (maximum ignorance)
        market_price = features.get("market_yes_price", 0.5)
        prior = market_price if market_price not in (0.0, 1.0) else 0.5

        updater = BayesianUpdater(prior=prior)

        # Get likelihood estimates from features
        updates = self._estimate_likelihoods(features, bundle)

        # Apply each update
        for description, l_yes, l_no in updates:
            updater.update(description, likelihood_yes=l_yes, likelihood_no=l_no)

        # Build reasoning from update history
        reasoning_parts = [f"Prior: {prior:.1%} (from market)"]
        for update in updater.history:
            direction = "+" if update.posterior > update.prior else "-"
            reasoning_parts.append(
                f"  [{direction}] {update.evidence_description}: "
                f"{update.prior:.1%} -> {update.posterior:.1%}"
            )
        reasoning_parts.append(f"Final: {updater.current:.1%}")

        # Confidence based on amount of evidence and update magnitude
        evidence_confidence = min(1.0, bundle.item_count / 20)
        update_magnitude = abs(updater.total_shift)
        confidence = evidence_confidence * 0.7 + min(update_magnitude * 2, 0.3)

        return Prediction(
            question_id=question.id,
            predictor_name=self.name,
            probability=round(updater.current, 3),
            confidence=round(min(0.9, confidence), 2),
            reasoning="\n".join(reasoning_parts),
            evidence_count=bundle.item_count,
        )
