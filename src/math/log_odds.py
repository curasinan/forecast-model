"""Log-odds (logit/sigmoid) transforms for bounded probability prices.

Prediction market prices are bounded [0, 1]. Standard models assume unbounded
real numbers. The logit transform maps probabilities to the real line where
diffusion models and linear operations apply correctly.

    logit(p) = ln(p / (1 - p))     maps (0,1) -> (-inf, +inf)
    sigmoid(x) = 1 / (1 + e^(-x))  maps (-inf, +inf) -> (0,1)

This is the same sigmoid at the heart of every neural network and logistic
regression. It guarantees prices stay in (0,1) by construction.
"""

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
    """Market price IS the implied probability in a prediction market.

    In Polymarket, YES price of $0.35 = 35% implied probability.
    This function exists for clarity and to clamp edge cases.
    """
    return max(0.01, min(0.99, price))
