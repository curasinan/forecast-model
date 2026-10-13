"""Market selector — find the best markets to analyze."""

import logging
from dataclasses import dataclass
from datetime import datetime

from src.collectors.polymarket import PolymarketCollector
from src.math.entropy import binary_entropy, opportunity_score

logger = logging.getLogger(__name__)

# Tech/AI keywords to search for
TECH_TAGS = [
    "technology", "ai", "artificial-intelligence",
    "openai", "google", "apple", "microsoft", "meta",
    "crypto", "bitcoin", "ethereum",
    "science", "space",
]


@dataclass
class MarketCandidate:
    """A market worth analyzing."""
    condition_id: str
    question: str
    slug: str
    yes_price: float
    volume_24h: float
    total_volume: float
    liquidity: float
    entropy: float
    opportunity_score: float
    end_date: str

    @property
    def days_to_expiry(self) -> float | None:
        """Days until market closes."""
        if not self.end_date:
            return None
        try:
            end = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            delta = end - datetime.now(end.tzinfo)
            return max(0, delta.total_seconds() / 86400)
        except Exception:
            return None


async def scan_markets(
    min_entropy: float = 0.5,
    min_volume: float = 100,
    min_liquidity: float = 50,
    max_results: int = 20,
) -> list[MarketCandidate]:
    """Scan Polymarket for high-opportunity tech markets."""
    collector = PolymarketCollector()

    # Fetch from multiple tags
    all_markets: list[dict] = []
    seen_ids: set[str] = set()

    for tag in TECH_TAGS:
        try:
            markets = await collector.search_markets(tag, limit=30)
            for m in markets:
                cid = m.get("condition_id", "")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    all_markets.append(m)
        except Exception as e:
            logger.warning(f"Failed to scan tag '{tag}': {e}")

    logger.info(f"Scanned {len(all_markets)} unique markets from {len(TECH_TAGS)} tags")

    # Score and filter
    candidates: list[MarketCandidate] = []
    for m in all_markets:
        yes_price = m.get("yes_price", 0.5)
        vol_24h = m.get("volume_24h", 0)
        total_vol = m.get("volume", 0)
        liquidity = m.get("liquidity", 0)

        entropy = binary_entropy(yes_price)
        opp_score = opportunity_score(yes_price, vol_24h)

        # Apply filters
        if entropy < min_entropy:
            continue
        if vol_24h < min_volume and total_vol < min_volume * 10:
            continue
        if liquidity < min_liquidity and liquidity > 0:
            continue

        # Skip closed markets
        if m.get("closed", False):
            continue

        candidates.append(MarketCandidate(
            condition_id=m.get("condition_id", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            yes_price=yes_price,
            volume_24h=vol_24h,
            total_volume=total_vol,
            liquidity=liquidity,
            entropy=entropy,
            opportunity_score=opp_score,
            end_date=m.get("end_date", ""),
        ))

    # Sort by opportunity score
    candidates.sort(key=lambda c: c.opportunity_score, reverse=True)
    return candidates[:max_results]


def filter_actionable(
    candidates: list[MarketCandidate],
    min_days_to_expiry: float = 7,
    max_price: float = 0.90,
    min_price: float = 0.10,
) -> list[MarketCandidate]:
    """Further filter to actionable markets."""
    actionable = []
    for c in candidates:
        # Price filter
        if c.yes_price > max_price or c.yes_price < min_price:
            continue

        # Expiry filter
        days = c.days_to_expiry
        if days is not None and days < min_days_to_expiry:
            continue

        actionable.append(c)

    return actionable
