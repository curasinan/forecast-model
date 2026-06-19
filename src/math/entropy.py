"""Shannon Entropy — measures uncertainty = opportunity.

    H = -Σ p_i × log₂(p_i)

Markets near $0.50 have maximum entropy (maximum uncertainty).
These are where our model can add the most value if better calibrated.

Markets near $0.95 have near-zero entropy — the outcome is almost certain.
Small edge, high adverse selection risk from informed traders.

Use entropy to SELECT which markets are worth our compute.
"""

import math


def binary_entropy(p: float) -> float:
    """Shannon entropy for a binary outcome.

    Args:
        p: Probability of YES (0-1). NO probability is 1-p.

    Returns:
        Entropy in bits. 0 = certain, 1.0 = maximum uncertainty (p=0.5).
    """
    p = max(1e-10, min(1 - 1e-10, p))
    q = 1 - p
    return -(p * math.log2(p) + q * math.log2(q))


def opportunity_score(yes_price: float, volume_24h: float) -> float:
    """Score a market by entropy × liquidity.

    High entropy (uncertain outcome) AND high volume (enough liquidity
    to trade) = best opportunity for our model to add value.

    Args:
        yes_price: Current YES token price (0-1)
        volume_24h: 24-hour trading volume in USD

    Returns:
        Opportunity score (higher = more attractive market).
    """
    entropy = binary_entropy(yes_price)

    # Normalize volume with log scale (avoids one massive market dominating)
    volume_factor = math.log1p(volume_24h) / math.log1p(1_000_000)  # Normalize to ~1M
    volume_factor = min(volume_factor, 2.0)  # Cap at 2x

    return entropy * volume_factor


def rank_markets(
    markets: list[dict],
    price_key: str = "yes_price",
    volume_key: str = "volume_24h",
) -> list[dict]:
    """Rank markets by opportunity score (highest first).

    Args:
        markets: List of market dicts with price and volume fields.
        price_key: Key for YES price in each dict.
        volume_key: Key for 24h volume in each dict.

    Returns:
        Same list sorted by opportunity score descending, with score added.
    """
    for market in markets:
        market["entropy"] = binary_entropy(market.get(price_key, 0.5))
        market["opportunity_score"] = opportunity_score(
            market.get(price_key, 0.5),
            market.get(volume_key, 0),
        )

    return sorted(markets, key=lambda m: m["opportunity_score"], reverse=True)
