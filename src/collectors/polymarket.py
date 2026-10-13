"""Polymarket CLOB API collector — fetches live market data."""

import logging
from datetime import datetime

import httpx

from src.collectors.base import BaseCollector
from src.config import settings
from src.models.evidence import EvidenceItem
from src.models.question import MarketData, Question

logger = logging.getLogger(__name__)

# Timeout for API calls
TIMEOUT = httpx.Timeout(15.0)


class PolymarketCollector(BaseCollector):
    """Fetches market data from Polymarket's APIs."""

    source_type = "polymarket"

    def __init__(self):
        self.gamma_base = settings.gamma_api_base
        self.clob_base = settings.polymarket_api_base

    async def collect(self, question: Question, max_results: int = 5) -> list[EvidenceItem]:
        """Collect market-as-evidence: current prices, volume, recent movement."""
        items: list[EvidenceItem] = []

        if question.market.market_id:
            # Direct lookup by market ID
            market_data = await self.get_market_by_id(question.market.market_id)
            if market_data:
                items.append(
                    EvidenceItem(
                        source_type=self.source_type,
                        source_name="Polymarket",
                        title=f"Market: {question.title}",
                        url=f"https://polymarket.com/event/{question.market.slug}",
                        content_snippet=(
                            f"YES: ${market_data.yes_price:.2f} | "
                            f"NO: ${market_data.no_price:.2f} | "
                            f"Volume 24h: ${market_data.volume_24h:,.0f} | "
                            f"Total Volume: ${market_data.total_volume:,.0f}"
                        ),
                    )
                )
        else:
            # Search by keywords
            for keyword in question.search_keywords[:2]:
                markets = await self.search_markets(keyword, limit=max_results)
                for m in markets:
                    items.append(
                        EvidenceItem(
                            source_type=self.source_type,
                            source_name="Polymarket",
                            title=m.get("question", m.get("title", "")),
                            url=f"https://polymarket.com/event/{m.get('slug', '')}",
                            content_snippet=(
                                f"YES: ${m.get('yes_price', 0):.2f} | "
                                f"Volume: ${m.get('volume', 0):,.0f}"
                            ),
                        )
                    )

        return items[:max_results]

    async def search_markets(
        self,
        query: str,
        limit: int = 10,
        tag: str = "",
    ) -> list[dict]:
        """Search Polymarket's Gamma API for markets matching a query."""
        params: dict = {"limit": limit, "active": "true", "closed": "false"}
        if query:
            params["tag_slug"] = query.lower().replace(" ", "-")

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                # Try tag-based search first
                resp = await client.get(f"{self.gamma_base}/markets", params=params)
                if resp.status_code == 200:
                    markets = resp.json()
                    if isinstance(markets, list):
                        return self._normalize_markets(markets)

                # Fallback: text search
                params.pop("tag_slug", None)
                params["_q"] = query
                resp = await client.get(f"{self.gamma_base}/markets", params=params)
                if resp.status_code == 200:
                    markets = resp.json()
                    if isinstance(markets, list):
                        return self._normalize_markets(markets)
        except Exception as e:
            logger.warning(f"Polymarket search failed for '{query}': {e}")

        return []

    async def get_market_by_id(self, condition_id: str) -> MarketData | None:
        """Fetch specific market data by condition ID."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(
                    f"{self.gamma_base}/markets",
                    params={"condition_id": condition_id},
                )
                if resp.status_code == 200:
                    markets = resp.json()
                    if markets and isinstance(markets, list) and len(markets) > 0:
                        m = markets[0]
                        return self._to_market_data(m)
        except Exception as e:
            logger.warning(f"Polymarket market fetch failed for {condition_id}: {e}")
        return None

    async def get_all_tech_markets(self, limit: int = 50) -> list[dict]:
        """Fetch tech/AI-related markets for scanning."""
        all_markets: list[dict] = []
        for tag in ["technology", "ai", "crypto", "science"]:
            markets = await self.search_markets(tag, limit=limit)
            all_markets.extend(markets)

        # Deduplicate by condition_id
        seen = set()
        unique = []
        for m in all_markets:
            cid = m.get("condition_id", m.get("id", ""))
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(m)

        return unique

    def _normalize_markets(self, raw_markets: list[dict]) -> list[dict]:
        """Normalize Gamma API response to consistent format."""
        normalized = []
        for m in raw_markets:
            # Handle both single-outcome and multi-outcome markets
            yes_price = 0.5
            tokens = m.get("tokens", [])
            if tokens:
                for t in tokens:
                    if t.get("outcome", "").upper() == "YES":
                        yes_price = float(t.get("price", 0.5))
                        break

            normalized.append({
                "condition_id": m.get("conditionId", m.get("condition_id", "")),
                "question": m.get("question", m.get("title", "")),
                "slug": m.get("slug", ""),
                "yes_price": yes_price,
                "no_price": 1 - yes_price,
                "volume": float(m.get("volume", 0) or 0),
                "volume_24h": float(m.get("volume24hr", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "end_date": m.get("endDate", m.get("end_date_iso", "")),
                "active": m.get("active", True),
                "closed": m.get("closed", False),
            })
        return normalized

    def _to_market_data(self, raw: dict) -> MarketData:
        """Convert raw API response to MarketData model."""
        yes_price = 0.5
        tokens = raw.get("tokens", [])
        if tokens:
            for t in tokens:
                if t.get("outcome", "").upper() == "YES":
                    yes_price = float(t.get("price", 0.5))
                    break

        return MarketData(
            market_id=raw.get("conditionId", raw.get("condition_id", "")),
            slug=raw.get("slug", ""),
            yes_price=yes_price,
            no_price=1 - yes_price,
            volume_24h=float(raw.get("volume24hr", 0) or 0),
            total_volume=float(raw.get("volume", 0) or 0),
            liquidity=float(raw.get("liquidity", 0) or 0),
            last_updated=datetime.now(),
        )
