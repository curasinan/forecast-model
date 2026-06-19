"""VPIN — Volume-Synchronized Probability of Informed Trading.

    VPIN = |V_buy - V_sell| / (V_buy + V_sell)

When buy and sell volume are balanced, flow is noise (uninformed traders).
When they diverge sharply, someone with private information is trading
aggressively in one direction.

VPIN ranges from 0 to 1:
    0.0 = perfectly balanced flow (all noise)
    0.5 = moderately imbalanced (some informed trading)
    1.0 = completely one-sided (fully informed flow)

When VPIN is high:
    - Widen the trust window on market price
    - Market price may NOT reflect true probability
    - Reduce Kelly sizing (informed traders may know something we don't)
    - Our model's edge estimate becomes less reliable

For Phase 1, we estimate VPIN from available Polymarket data.
Phase 3+ would use real-time orderbook data via WebSocket.
"""

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
    """Estimate VPIN from available market data.

    Without real-time orderbook data, we approximate VPIN using:
    - Large price movements with high volume = likely informed trading
    - Stable prices with high volume = balanced flow (low VPIN)
    - Large price movements with low volume = noise or illiquid market

    Args:
        current_price: Current YES token price (0-1)
        volume_24h: 24-hour trading volume in USD
        price_change_24h: Absolute price change over 24h (e.g., 0.05 = 5 cent move)

    Returns:
        Estimated VPIN (0-1).
    """
    if volume_24h < 100:
        # Very low volume — can't estimate VPIN meaningfully
        return 0.0

    # Price movement relative to the price level
    # A $0.05 move at $0.50 is 10%, but at $0.95 it's more significant
    price_sensitivity = min(current_price, 1 - current_price)  # Distance from edge
    if price_sensitivity < 0.01:
        price_sensitivity = 0.01

    relative_move = abs(price_change_24h) / price_sensitivity

    # Volume-adjusted movement: big moves with big volume = informed trading
    # Normalize volume (log scale to handle wide ranges)
    import math
    vol_factor = math.log1p(volume_24h) / math.log1p(100_000)  # Normalize to ~100k
    vol_factor = min(vol_factor, 2.0)

    # VPIN estimate: combines movement magnitude with volume
    vpin = min(1.0, relative_move * vol_factor * 0.3)

    return round(vpin, 3)


def assess_vpin(vpin: float) -> dict:
    """Interpret a VPIN value.

    Returns:
        Dict with level, description, and recommended actions.
    """
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
