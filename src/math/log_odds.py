"""Log-odds (logit/sigmoid) transforms for bounded probability prices."""

import math


def logit(p: float) -> float:
    """Convert probability to log-odds. p must be in (0, 1)."""
    p = max(1e-10, min(1 - 1e-10, p))  # Clamp to avoid log(0)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    """Convert log-odds back to probability. Always returns (0, 1)."""
    if x > 500:
        return 1.0 - 1e-10
    if x < -500:
        return 1e-10
    return 1.0 / (1.0 + math.exp(-x))


def odds_from_prob(p: float) -> float:
    """Convert probability to decimal odds. p=0.4 -> 2.5 (pays $2.50 per $1)."""
    p = max(1e-10, min(1 - 1e-10, p))
    return 1.0 / p


def implied_probability(price: float) -> float:
    """Market price IS the implied probability in a prediction market."""
    return max(0.01, min(0.99, price))
