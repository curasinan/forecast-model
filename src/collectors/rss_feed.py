"""RSS feed collector — free, no API key required."""

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser

from src.collectors.base import BaseCollector
from src.models.evidence import EvidenceItem
from src.models.question import Question

logger = logging.getLogger(__name__)

# Feeds that support keyword search via URL parameter
SEARCH_FEEDS = [
    "https://news.google.com/rss/search?q={query}+when:7d&hl=en-US&gl=US&ceid=US:en",
    "https://hnrss.org/newest?q={query}&count=20",
]

# Static tech feeds (no search, we filter client-side)
STATIC_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
]


def _parse_date(entry: dict) -> datetime | None:
    """Try to extract a datetime from an RSS entry."""
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except Exception:
                try:
                    return datetime.fromisoformat(raw)
                except Exception:
                    pass
    return None


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """Check if text contains any of the keywords (case-insensitive)."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


class RSSFeedCollector(BaseCollector):
    """Collects evidence from RSS feeds."""

    source_type = "rss"

    async def collect(self, question: Question, max_results: int = 20) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []

        # Search feeds: substitute keywords into URL
        for keyword in question.search_keywords[:3]:  # Limit to avoid rate issues
            for feed_template in SEARCH_FEEDS:
                feed_url = feed_template.format(query=keyword.replace(" ", "+"))
                try:
                    parsed = feedparser.parse(feed_url)
                    feed_name = parsed.feed.get("title", "RSS")
                    for entry in parsed.entries[:max_results]:
                        items.append(
                            EvidenceItem(
                                source_type=self.source_type,
                                source_name=feed_name,
                                title=entry.get("title", ""),
                                url=entry.get("link", ""),
                                content_snippet=(entry.get("summary", "") or "")[:500],
                                published_at=_parse_date(entry),
                            )
                        )
                except Exception as e:
                    logger.warning(f"RSS feed failed for {feed_url}: {e}")

        # Static feeds: fetch and filter by keywords
        for feed_name, feed_url in STATIC_FEEDS:
            try:
                parsed = feedparser.parse(feed_url)
                for entry in parsed.entries[:50]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "") or ""
                    if _matches_keywords(f"{title} {summary}", question.search_keywords):
                        items.append(
                            EvidenceItem(
                                source_type=self.source_type,
                                source_name=feed_name,
                                title=title,
                                url=entry.get("link", ""),
                                content_snippet=summary[:500],
                                published_at=_parse_date(entry),
                            )
                        )
            except Exception as e:
                logger.warning(f"Static RSS feed failed for {feed_name}: {e}")

        # Sort by date (newest first) and limit
        items.sort(key=lambda x: (x.published_at or datetime.min).timestamp(), reverse=True)
        return items[:max_results]
