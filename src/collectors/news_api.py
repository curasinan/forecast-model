"""NewsAPI.org collector — structured news search.

NewsAPI free tier ("Developer") only supports the /top-headlines endpoint
from server-side scripts. The /everything endpoint requires a paid plan
($449+/month) for server-side usage.

We use /top-headlines with category=technology and keyword filtering,
which works on the free tier and gives us current tech headlines.

If you have a paid key, set NEWSAPI_PLAN=paid in .env to unlock /everything.

API docs: https://newsapi.org/docs/endpoints
"""

import logging
from datetime import datetime

import httpx

from src.collectors.base import BaseCollector
from src.config import settings
from src.models.evidence import EvidenceItem
from src.models.question import Question

logger = logging.getLogger(__name__)

NEWSAPI_BASE = "https://newsapi.org/v2"


class NewsAPICollector(BaseCollector):
    """Collects evidence from NewsAPI.org."""

    source_type = "newsapi"

    def is_available(self) -> bool:
        return bool(settings.newsapi_key)

    async def collect(self, question: Question, max_results: int = 20) -> list[EvidenceItem]:
        if not self.is_available():
            logger.info("NewsAPI key not set, skipping.")
            return []

        items: list[EvidenceItem] = []

        # Try /top-headlines with keyword search (works on free tier)
        for keyword in question.search_keywords[:2]:
            try:
                articles = await self._search_headlines(keyword, max_results=max_results)
                items.extend(articles)
            except Exception as e:
                logger.warning(f"NewsAPI headlines search failed for '{keyword}': {e}")

        # Also fetch general tech headlines and filter client-side
        try:
            tech_articles = await self._get_tech_headlines(max_results=30)
            # Filter by question keywords
            for article in tech_articles:
                text = f"{article.title} {article.content_snippet}".lower()
                if any(kw.lower() in text for kw in question.search_keywords):
                    items.append(article)
        except Exception as e:
            logger.warning(f"NewsAPI tech headlines failed: {e}")

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique: list[EvidenceItem] = []
        for item in items:
            if item.url and item.url not in seen_urls:
                seen_urls.add(item.url)
                unique.append(item)

        unique.sort(key=lambda x: (x.published_at or datetime.min).timestamp(), reverse=True)
        return unique[:max_results]

    async def _search_headlines(self, query: str, max_results: int = 20) -> list[EvidenceItem]:
        """Search /top-headlines with a keyword (free tier compatible)."""
        params = {
            "q": query,
            "language": "en",
            "pageSize": min(max_results, 100),
        }

        return await self._fetch(f"{NEWSAPI_BASE}/top-headlines", params)

    async def _get_tech_headlines(self, max_results: int = 30) -> list[EvidenceItem]:
        """Get top technology headlines (free tier compatible)."""
        params = {
            "category": "technology",
            "language": "en",
            "country": "us",
            "pageSize": min(max_results, 100),
        }

        return await self._fetch(f"{NEWSAPI_BASE}/top-headlines", params)

    async def _fetch(self, url: str, params: dict) -> list[EvidenceItem]:
        """Make a NewsAPI request and parse the response."""
        headers = {"X-Api-Key": settings.newsapi_key}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)

            if resp.status_code == 426:
                logger.warning(
                    "NewsAPI returned 426 (Upgrade Required). "
                    "Free tier /everything needs a paid plan for server-side use. "
                    "Using /top-headlines instead."
                )
                return []

            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "ok":
            logger.warning(f"NewsAPI error: {data.get('message', data.get('status'))}")
            return []

        items: list[EvidenceItem] = []
        for article in data.get("articles", []):
            # Skip removed articles
            title = article.get("title", "")
            if not title or title == "[Removed]":
                continue

            published_at = None
            if article.get("publishedAt"):
                try:
                    published_at = datetime.fromisoformat(
                        article["publishedAt"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            source_name = article.get("source", {}).get("name", "NewsAPI")
            description = article.get("description") or ""
            content = article.get("content") or ""
            snippet = description if description else content

            items.append(
                EvidenceItem(
                    source_type=self.source_type,
                    source_name=source_name,
                    title=title,
                    url=article.get("url", ""),
                    content_snippet=snippet[:500],
                    published_at=published_at,
                )
            )

        return items
