"""Bayesian updating — the core loop of the entire prediction system.

    P(A|B) = P(B|A) × P(A) / P(B)

Start with a prior belief. Observe evidence. Update rationally.
Each new piece of evidence shifts the probability in a precise, calculated way.

The BayesianUpdater tracks the full history of updates so you can see
exactly how each piece of evidence changed the estimate.
"""

from dataclasses import dataclass, field


@dataclass
class BayesianUpdate:
    """Record of a single Bayesian update."""

    evidence_description: str
    likelihood_yes: float  # P(evidence | YES) - how likely is this evidence if event happens
    likelihood_no: float  # P(evidence | NO) - how likely is this evidence if event doesn't happen
    prior: float  # Probability before this update
    posterior: float  # Probability after this update


class BayesianUpdater:
    """Tracks a probability estimate and updates it with Bayes' theorem.

    Usage:
        updater = BayesianUpdater(prior=0.3)
        updater.update("CEO confirms launch timeline", likelihood_yes=0.9, likelihood_no=0.1)
        updater.update("Key engineer leaves company", likelihood_yes=0.3, likelihood_no=0.6)
        print(updater.current)  # Updated probability
        print(updater.history)  # Full update trail
    """

    def __init__(self, prior: float = 0.5):
        self._current = max(0.01, min(0.99, prior))
        self.history: list[BayesianUpdate] = []

    @property
    def current(self) -> float:
        """Current posterior probability."""
        return self._current

    def update(
        self,
        evidence_description: str,
        likelihood_yes: float,
        likelihood_no: float,
    ) -> float:
        """Apply Bayes' theorem with new evidence.

        Args:
            evidence_description: What the evidence is (for logging)
            likelihood_yes: P(evidence | event happens). How likely would we see
                this evidence if the event is going to happen?
            likelihood_no: P(evidence | event doesn't happen). How likely would
                we see this evidence if the event is NOT going to happen?

        Returns:
            Updated posterior probability.
        """
        prior = self._current

        # Bayes' theorem:
        # P(YES|evidence) = P(evidence|YES) * P(YES) / P(evidence)
        # P(evidence) = P(evidence|YES)*P(YES) + P(evidence|NO)*P(NO)
        p_evidence = (likelihood_yes * prior) + (likelihood_no * (1 - prior))

        if p_evidence < 1e-10:
            posterior = prior  # No update if evidence is impossible under both hypotheses
        else:
            posterior = (likelihood_yes * prior) / p_evidence

        # Clamp to avoid 0 or 1 (always leave room for updating)
        posterior = max(0.01, min(0.99, posterior))

        self.history.append(
            BayesianUpdate(
                evidence_description=evidence_description,
                likelihood_yes=likelihood_yes,
                likelihood_no=likelihood_no,
                prior=prior,
                posterior=posterior,
            )
        )

        self._current = posterior
        return posterior

    def reset(self, prior: float = 0.5) -> None:
        """Reset to a new prior, clearing history."""
        self._current = max(0.01, min(0.99, prior))
        self.history.clear()

    @property
    def total_shift(self) -> float:
        """How much the probability has shifted from the original prior."""
        if not self.history:
            return 0.0
        return self._current - self.history[0].prior


@dataclass
class NaiveBayesFeatures:
    """Simple Naive Bayes classifier for binary outcomes using feature likelihoods.

    P(YES | features) ∝ P(YES) × ∏ P(feature_i | YES)

    This gives a fast, transparent baseline probability estimate.
    """

    # Feature likelihoods: {feature_name: (P(feature|YES), P(feature|NO))}
    feature_likelihoods: dict[str, tuple[float, float]] = field(default_factory=dict)

    def classify(self, features: dict[str, float], prior_yes: float = 0.5) -> float:
        """Compute P(YES | features) using Naive Bayes.

        Args:
            features: Dict of {feature_name: observed_value}. Values are treated
                as continuous — we use Gaussian likelihood approximation.
            prior_yes: Prior probability of YES.

        Returns:
            Posterior probability of YES.
        """
        log_odds_yes = 0.0
        log_odds_no = 0.0

        import math

        log_odds_yes = math.log(max(1e-10, prior_yes))
        log_odds_no = math.log(max(1e-10, 1 - prior_yes))

        for feat_name, (p_yes, p_no) in self.feature_likelihoods.items():
            if feat_name in features:
                # Use the feature value as a weight on the likelihood
                val = features[feat_name]
                # Simple: interpolate between likelihood and uniform based on value
                log_odds_yes += math.log(max(1e-10, p_yes)) * abs(val)
                log_odds_no += math.log(max(1e-10, p_no)) * abs(val)

        # Convert back to probability via softmax
        max_log = max(log_odds_yes, log_odds_no)
        p_yes_unnorm = math.exp(log_odds_yes - max_log)
        p_no_unnorm = math.exp(log_odds_no - max_log)
        total = p_yes_unnorm + p_no_unnorm

        if total < 1e-10:
            return prior_yes

        posterior = p_yes_unnorm / total
        return max(0.01, min(0.99, posterior))
