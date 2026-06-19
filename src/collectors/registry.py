"""Collector registry — discovers, manages, and runs all collectors."""

import logging

from src.collectors.base import BaseCollector
from src.models.evidence import EvidenceBundle, EvidenceItem
from src.models.question import Question

logger = logging.getLogger(__name__)


class CollectorRegistry:
    """Orchestrates all registered data collectors."""

    def __init__(self):
        self._collectors: list[BaseCollector] = []

    def register(self, collector: BaseCollector) -> None:
        if collector.is_available():
            self._collectors.append(collector)
            logger.info(f"Registered collector: {collector.source_type}")
        else:
            logger.warning(f"Collector {collector.source_type} not available (missing credentials?)")

    @property
    def active_collectors(self) -> list[str]:
        return [c.source_type for c in self._collectors]

    async def collect_all(self, question: Question, max_per_source: int = 20) -> EvidenceBundle:
        """Run all available collectors and merge results."""
        all_items: list[EvidenceItem] = []

        for collector in self._collectors:
            try:
                items = await collector.collect(question, max_results=max_per_source)
                all_items.extend(items)
                logger.info(
                    f"Collector {collector.source_type}: {len(items)} items for '{question.id}'"
                )
            except Exception as e:
                logger.warning(f"Collector {collector.source_type} failed: {e}")

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_items: list[EvidenceItem] = []
        for item in all_items:
            if not item.url or item.url not in seen_urls:
                if item.url:
                    seen_urls.add(item.url)
                unique_items.append(item)

        return EvidenceBundle(question_id=question.id, items=unique_items)
