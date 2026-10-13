"""VPIN — Volume-Synchronized Probability of Informed Trading."""

import logging

logger = logging.getLogger(__name__)

# VPIN thresholds
VPIN_LOW = 0.3  # Normal market activity
VPIN_MEDIUM = 0.5  # Some informed trading likely
VPIN_HIGH = 0.7  # Significant informed trading


def estimate_vpin_from_price_movement(
    current_price: float,
    volume_24h: float,
    price_change_24h: float = 0.0,
) -> float:
    """Estimate VPIN from available market data."""
    if volume_24h < 100:
        # Very low volume — can't estimate VPIN meaningfully
        return 0.0

    price_sensitivity = min(current_price, 1 - current_price)  # Distance from edge
    if price_sensitivity < 0.01:
        price_sensitivity = 0.01

    relative_move = abs(price_change_24h) / price_sensitivity

    import math
    vol_factor = math.log1p(volume_24h) / math.log1p(100_000)  # Normalize to ~100k
    vol_factor = min(vol_factor, 2.0)

    # VPIN estimate: combines movement magnitude with volume
    vpin = min(1.0, relative_move * vol_factor * 0.3)

    return round(vpin, 3)


def assess_vpin(vpin: float) -> dict:
    """Interpret a VPIN value."""
    if vpin < VPIN_LOW:
        return {
            "level": "low",
            "vpin": vpin,
            "description": "Normal market activity. Flow appears uninformed.",
            "trust_market_price": True,
            "kelly_multiplier": 1.0,  # Full Kelly confidence
        }
    elif vpin < VPIN_MEDIUM:
        return {
            "level": "medium",
            "vpin": vpin,
            "description": "Moderate imbalance. Some informed trading possible.",
            "trust_market_price": True,
            "kelly_multiplier": 0.75,  # Reduce position by 25%
        }
    elif vpin < VPIN_HIGH:
        return {
            "level": "elevated",
            "vpin": vpin,
            "description": "Significant imbalance. Informed traders likely active.",
            "trust_market_price": False,
            "kelly_multiplier": 0.5,  # Halve position
        }
    else:
        return {
            "level": "high",
            "vpin": vpin,
            "description": "Extreme imbalance. Market likely being moved by informed flow.",
            "trust_market_price": False,
            "kelly_multiplier": 0.25,  # Quarter position — high adverse selection risk
        }
