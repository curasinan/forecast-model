"""Bayesian updating — the core loop of the entire prediction system."""

from dataclasses import dataclass, field


@dataclass
class BayesianUpdate:
    """Record of a single Bayesian update."""

    evidence_description: str
    likelihood_yes: float
    likelihood_no: float
    prior: float  # Probability before this update
    posterior: float  # Probability after this update


class BayesianUpdater:
    """Tracks a probability estimate and updates it with Bayes' theorem."""

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
        """Apply Bayes' theorem with new evidence."""
        prior = self._current

        p_evidence = (likelihood_yes * prior) + (likelihood_no * (1 - prior))

        if p_evidence < 1e-10:
            posterior = prior
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
    """Simple Naive Bayes classifier for binary outcomes using feature likelihoods."""

    # Feature likelihoods: {feature_name: (P(feature|YES), P(feature|NO))}
    feature_likelihoods: dict[str, tuple[float, float]] = field(default_factory=dict)

    def classify(self, features: dict[str, float], prior_yes: float = 0.5) -> float:
        """Compute P(YES | features) using Naive Bayes."""
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
