"""Shannon Entropy — measures uncertainty = opportunity."""

import math


def binary_entropy(p: float) -> float:
    """Shannon entropy for a binary outcome."""
    p = max(1e-10, min(1 - 1e-10, p))
    q = 1 - p
    return -(p * math.log2(p) + q * math.log2(q))


def opportunity_score(yes_price: float, volume_24h: float) -> float:
    """Score a market by entropy × liquidity."""
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
    """Rank markets by opportunity score (highest first)."""
    for market in markets:
        market["entropy"] = binary_entropy(market.get(price_key, 0.5))
        market["opportunity_score"] = opportunity_score(
            market.get(price_key, 0.5),
            market.get(volume_key, 0),
        )

    return sorted(markets, key=lambda m: m["opportunity_score"], reverse=True)
