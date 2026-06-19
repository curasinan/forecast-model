"""Monte Carlo simulation engine for probability estimation and risk analysis.

Instead of trusting a single point estimate ("our model says 62%"), Monte Carlo
simulates thousands of possible outcomes accounting for estimation uncertainty.

Key outputs:
    - Probability distribution (not just a point estimate)
    - Confidence intervals (5th to 95th percentile)
    - Edge distribution (how often do we actually have an edge?)
    - Worst-case analysis (what happens if we're wrong?)

This feeds directly into empirical Kelly sizing: instead of betting based on
our best guess, we size positions based on the DISTRIBUTION of possible edges.

The math:
    1. Model our probability estimate as a Beta(alpha, beta) distribution
       - Beta is the natural distribution for probabilities (bounded [0,1])
       - alpha, beta parameters encode our mean estimate and confidence
    2. Sample N times from this distribution
    3. For each sample, calculate edge vs market, EV, and Kelly fraction
    4. Use the DISTRIBUTION of Kelly fractions to size conservatively

Why Beta distribution?
    - Bounded on [0, 1] — perfect for probabilities
    - Flexible shape: can be uniform, bell-curved, or skewed
    - Conjugate prior for Bernoulli outcomes (mathematically elegant)
    - Mean = alpha / (alpha + beta), Variance = alpha*beta / ((a+b)^2 * (a+b+1))
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class MonteCarloResult:
    """Results from a Monte Carlo probability simulation."""

    # Point estimates
    mean: float
    median: float
    std: float

    # Confidence intervals
    ci_5: float   # 5th percentile (worst case)
    ci_25: float  # 25th percentile
    ci_75: float  # 75th percentile
    ci_95: float  # 95th percentile (best case)

    # Distribution shape
    skewness: float  # Positive = right-skewed, negative = left-skewed

    # Raw samples (for downstream analysis)
    samples: np.ndarray

    @property
    def ci_90_width(self) -> float:
        """Width of 90% confidence interval (ci_95 - ci_5)."""
        return self.ci_95 - self.ci_5

    @property
    def coefficient_of_variation(self) -> float:
        """CV = std / mean. Higher = more uncertain relative to estimate."""
        if self.mean < 1e-10:
            return float("inf")
        return self.std / self.mean


@dataclass
class EdgeDistribution:
    """Distribution of edge estimates from Monte Carlo simulation."""

    mean_edge: float
    median_edge: float
    std_edge: float

    # What fraction of simulations show positive edge?
    prob_positive_edge: float

    # Edge confidence intervals
    edge_ci_5: float
    edge_ci_95: float

    # EV distribution
    mean_ev: float
    prob_positive_ev: float

    # Kelly distribution
    mean_kelly: float
    median_kelly: float
    kelly_ci_5: float
    kelly_ci_95: float

    # Coefficient of variation of edge (used for empirical Kelly)
    cv_edge: float

    # Raw arrays
    edge_samples: np.ndarray
    ev_samples: np.ndarray
    kelly_samples: np.ndarray


def probability_to_beta_params(
    probability: float,
    confidence: float,
) -> tuple[float, float]:
    """Convert a probability estimate + confidence into Beta distribution parameters.

    The confidence controls how "concentrated" the distribution is around
    the probability estimate. Higher confidence = tighter distribution.

    Args:
        probability: Point estimate of probability (0-1)
        confidence: How confident we are in this estimate (0-1)
            0.1 = very uncertain (wide distribution)
            0.5 = moderate confidence
            0.9 = very confident (tight distribution)

    Returns:
        (alpha, beta) parameters for the Beta distribution.

    Math:
        concentration = f(confidence) controls the "peakedness"
        alpha = probability * concentration
        beta = (1 - probability) * concentration
        Mean of Beta = alpha/(alpha+beta) = probability (exactly our estimate)
    """
    # Map confidence to concentration parameter
    # Low confidence -> low concentration (2-5) -> wide distribution
    # High confidence -> high concentration (50-200) -> tight distribution
    min_concentration = 2.0
    max_concentration = 200.0

    # Exponential mapping gives more resolution at low confidence
    concentration = min_concentration + (max_concentration - min_concentration) * (confidence ** 2)

    # Clamp probability away from exact 0 or 1
    p = max(0.01, min(0.99, probability))

    alpha = p * concentration
    beta_param = (1.0 - p) * concentration

    # Ensure valid parameters (both must be > 0)
    alpha = max(0.1, alpha)
    beta_param = max(0.1, beta_param)

    return alpha, beta_param


def simulate_probability(
    base_probability: float,
    confidence: float = 0.5,
    n_simulations: int = 10_000,
    seed: int | None = None,
) -> MonteCarloResult:
    """Simulate probability distribution accounting for estimation uncertainty.

    Instead of saying "our model estimates 62%", this produces:
    "our estimate is 62% +/- 8%, with 90% CI [48%, 75%]"

    Args:
        base_probability: Our point estimate (0-1)
        confidence: How confident we are (0-1). Lower = wider distribution.
        n_simulations: Number of Monte Carlo samples
        seed: Random seed for reproducibility

    Returns:
        MonteCarloResult with full distribution statistics.
    """
    rng = np.random.default_rng(seed)

    alpha, beta_param = probability_to_beta_params(base_probability, confidence)
    samples = rng.beta(alpha, beta_param, size=n_simulations)

    # Calculate skewness manually (avoid scipy dependency for this)
    mean = float(np.mean(samples))
    std = float(np.std(samples))
    if std > 1e-10:
        skewness = float(np.mean(((samples - mean) / std) ** 3))
    else:
        skewness = 0.0

    return MonteCarloResult(
        mean=mean,
        median=float(np.median(samples)),
        std=std,
        ci_5=float(np.percentile(samples, 5)),
        ci_25=float(np.percentile(samples, 25)),
        ci_75=float(np.percentile(samples, 75)),
        ci_95=float(np.percentile(samples, 95)),
        skewness=skewness,
        samples=samples,
    )


def simulate_edge(
    model_probability: float,
    model_confidence: float,
    market_price: float,
    n_simulations: int = 10_000,
    seed: int | None = None,
) -> EdgeDistribution:
    """Simulate the distribution of edges between our model and the market.

    This is the core of empirical Kelly: instead of computing Kelly from
    a single edge estimate, we compute Kelly for EACH simulated probability
    and look at the distribution.

    Args:
        model_probability: Our model's point estimate
        model_confidence: Confidence in the estimate (0-1)
        market_price: Current market YES price
        n_simulations: Number of simulations
        seed: Random seed

    Returns:
        EdgeDistribution with edge, EV, and Kelly statistics.
    """
    # Simulate our probability uncertainty
    mc_result = simulate_probability(
        base_probability=model_probability,
        confidence=model_confidence,
        n_simulations=n_simulations,
        seed=seed,
    )

    prob_samples = mc_result.samples

    # For each simulated probability, calculate edge vs market
    edge_samples = prob_samples - market_price

    # EV for each simulation: EV = p * (1 - market_price) - (1 - p) * market_price
    ev_samples = prob_samples * (1.0 - market_price) - (1.0 - prob_samples) * market_price

    # Kelly for each simulation: f = (b*p - q) / b where b = (1/market_price) - 1
    if market_price > 0.01 and market_price < 0.99:
        b = (1.0 / market_price) - 1.0
        kelly_samples = (b * prob_samples - (1.0 - prob_samples)) / b
        kelly_samples = np.maximum(0.0, kelly_samples)  # No negative Kelly
    else:
        kelly_samples = np.zeros(n_simulations)

    # Coefficient of variation of edge (key input for empirical Kelly)
    mean_edge = float(np.mean(edge_samples))
    std_edge = float(np.std(edge_samples))
    cv_edge = std_edge / abs(mean_edge) if abs(mean_edge) > 1e-10 else float("inf")

    return EdgeDistribution(
        mean_edge=mean_edge,
        median_edge=float(np.median(edge_samples)),
        std_edge=std_edge,
        prob_positive_edge=float(np.mean(edge_samples > 0)),
        edge_ci_5=float(np.percentile(edge_samples, 5)),
        edge_ci_95=float(np.percentile(edge_samples, 95)),
        mean_ev=float(np.mean(ev_samples)),
        prob_positive_ev=float(np.mean(ev_samples > 0)),
        mean_kelly=float(np.mean(kelly_samples)),
        median_kelly=float(np.median(kelly_samples)),
        kelly_ci_5=float(np.percentile(kelly_samples, 5)),
        kelly_ci_95=float(np.percentile(kelly_samples, 95)),
        cv_edge=cv_edge,
        edge_samples=edge_samples,
        ev_samples=ev_samples,
        kelly_samples=kelly_samples,
    )


def simulate_bankroll_paths(
    edge: float,
    kelly_fraction: float,
    n_bets: int = 100,
    n_paths: int = 1_000,
    initial_bankroll: float = 1000.0,
    seed: int | None = None,
) -> dict:
    """Simulate bankroll evolution over multiple bets.

    Shows what happens to your bankroll if you repeatedly bet with a given
    edge and Kelly fraction. Useful for understanding the real-world impact
    of position sizing.

    Args:
        edge: Expected edge per bet (model_prob - market_price)
        kelly_fraction: Fraction of bankroll to bet each time
        n_bets: Number of sequential bets to simulate
        n_paths: Number of independent bankroll paths
        initial_bankroll: Starting capital
        seed: Random seed

    Returns:
        Dict with bankroll statistics at each step.
    """
    rng = np.random.default_rng(seed)

    # True win probability = market_price + edge (approximately)
    # For simplicity, assume market_price ~ 0.5 (adjustable)
    win_prob = 0.5 + edge

    # Simulate outcomes: 1 = win, 0 = loss
    outcomes = rng.random((n_paths, n_bets)) < win_prob

    # Track bankroll evolution
    bankrolls = np.full((n_paths, n_bets + 1), initial_bankroll)

    for t in range(n_bets):
        bet_size = bankrolls[:, t] * kelly_fraction
        # Win: gain bet_size * (payout - 1), Loss: lose bet_size
        # Simplified: win doubles the bet, loss loses it
        gains = np.where(outcomes[:, t], bet_size, -bet_size)
        bankrolls[:, t + 1] = np.maximum(0.0, bankrolls[:, t] + gains)

    # Final bankroll statistics
    final = bankrolls[:, -1]

    return {
        "mean_final": float(np.mean(final)),
        "median_final": float(np.median(final)),
        "ci_5_final": float(np.percentile(final, 5)),
        "ci_95_final": float(np.percentile(final, 95)),
        "prob_ruin": float(np.mean(final < initial_bankroll * 0.01)),  # < 1% of start
        "prob_profit": float(np.mean(final > initial_bankroll)),
        "max_drawdown_mean": float(np.mean(_max_drawdowns(bankrolls))),
        "bankroll_paths": bankrolls,
    }


def _max_drawdowns(bankrolls: np.ndarray) -> np.ndarray:
    """Calculate maximum drawdown for each bankroll path.

    Drawdown = (peak - current) / peak
    """
    n_paths = bankrolls.shape[0]
    max_dd = np.zeros(n_paths)

    for i in range(n_paths):
        path = bankrolls[i]
        running_max = np.maximum.accumulate(path)
        drawdowns = (running_max - path) / np.maximum(running_max, 1e-10)
        max_dd[i] = np.max(drawdowns)

    return max_dd
