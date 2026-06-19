"""Market feature extractor — signals from Polymarket price/volume data.

Technical indicators on prediction market price history:
- RSI: Momentum oscillator. >70 = overbought, <30 = oversold
- Volatility: Std deviation of price returns over a window
- Volume: Trading activity as proxy for information flow
"""

from src.features.base import BaseFeatureExtractor
from src.math.entropy import binary_entropy
from src.math.log_odds import logit
from src.models.evidence import EvidenceBundle
from src.models.question import MarketData


class MarketFeatureExtractor(BaseFeatureExtractor):
    """Extracts features from Polymarket data."""

    def __init__(self, market: MarketData):
        self.market = market

    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        m = self.market
        yes_price = max(0.01, min(0.99, m.yes_price))

        return {
            "market_yes_price": yes_price,
            "market_no_price": 1.0 - yes_price,
            "market_log_odds": logit(yes_price),
            "market_entropy": binary_entropy(yes_price),
            "market_volume_24h": m.volume_24h,
            "market_total_volume": m.total_volume,
            "market_liquidity": m.liquidity,
            # Volume intensity: high volume relative to liquidity = active trading
            "volume_intensity": (
                m.volume_24h / max(1.0, m.liquidity) if m.liquidity > 0 else 0.0
            ),
        }
